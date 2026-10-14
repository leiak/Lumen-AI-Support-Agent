"""Unit tests for HdbscanClusterer.

Clusterer contracts under test:

- Clusters dense cones against a background of scattered random unit
  vectors (HDBSCAN is density-based — it needs a denser region against
  a less dense background to find a cluster).
- Filters groups below ``min_cluster_size`` via the post-fit size guard
  (independent of HDBSCAN's internal noise label).
- Filters groups above ``max_cluster_size`` (mega-clusters).
- For each surviving cluster, ``centroid_idx`` is the dense point
  closest to the cluster mean — never an outlier.
- L2-normalizes the input matrix internally, so 2 clusters with
  wildly different magnitudes still produce 2 clusters.
- Survives zero-norm rows (drops them; never returns NaN).
- Empty / single-point / below-min-cluster-size inputs return ``[]``.

Why 2048-dim fixtures? The production embedding model for the demo
tenant is Doubao ``doubao-embedding-vision`` which emits 2048-dim
vectors. Using 2048-dim in tests (a) exercises the dimensionality the
real pipeline runs at, and (b) mirrors the unit-sphere topology the
clusterer is designed for.

Why "tight cluster + scattered noise" and not "3 tight cones"?
HDBSCAN is a density-based algorithm: it needs a contrast between
dense regions and sparse regions to identify clusters. A test with
ONLY dense groups (e.g. 3 cones, no scattered points) gives HDBSCAN
no reference density to compare against, and it correctly returns 0
clusters — which is not a bug, it's a property of density-based
clustering. The production data has plenty of random/mixed embeddings
forming the sparse background.

HDBSCAN is deterministic given the input data + a seeded numpy
generator, so every test uses ``np.random.default_rng(42)`` to keep
outcomes stable across runs.
"""
from __future__ import annotations

import numpy as np

from history_mining.clusterer import HdbscanClusterer

# Production dimensionality for the demo tenant's Doubao embedding model.
_DIM = 2048


def _unit_direction(rng: np.random.Generator, dim: int = _DIM) -> np.ndarray:
    """Return a uniformly-distributed unit vector (Rayleigh-norm then unit)."""
    v = rng.normal(0, 1, dim)
    return v / np.linalg.norm(v)


def _tight_cone(
    rng: np.random.Generator,
    *,
    center: np.ndarray,
    n: int,
    max_angle: float = 0.3,
    dim: int = _DIM,
) -> np.ndarray:
    """Generate ``n`` points clustered tightly around ``center`` (unit vector).

    Each point sits in a random plane through ``center`` at an angle
    in ``[0, max_angle]`` — i.e. on the unit sphere within a cone of
    half-angle ``max_angle``. ``max_angle=0.3 rad ≈ 17°`` matches the
    typical within-cluster spread we see in real Doubao embeddings.
    """
    points = np.empty((n, dim))
    for i in range(n):
        ortho = rng.normal(0, 1, dim)
        ortho = ortho - (ortho @ center) * center
        ortho /= np.linalg.norm(ortho)
        angle = max_angle * rng.random()
        points[i] = center * np.cos(angle) + ortho * np.sin(angle)
    return points


def _scattered_noise(
    rng: np.random.Generator, *, n: int, dim: int = _DIM
) -> np.ndarray:
    """Generate ``n`` random unit vectors — the sparse background."""
    v = rng.normal(0, 1, (n, dim))
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def _mixed_data(
    rng: np.random.Generator,
    *,
    cluster_sizes: list[tuple[int, int]],  # [(n_points, max_angle), ...]
    noise_n: int,
) -> np.ndarray:
    """Build a dataset with several dense cones + scattered noise background.

    Mirrors the production data shape: a few dense topic clusters
    (semantically similar questions) plus a long tail of random
    one-off questions that form the noise distribution.
    """
    parts: list[np.ndarray] = []
    for n, max_angle in cluster_sizes:
        center = _unit_direction(rng)
        parts.append(_tight_cone(rng, center=center, n=n, max_angle=max_angle))
    if noise_n > 0:
        parts.append(_scattered_noise(rng, n=noise_n))
    return np.vstack(parts)


# ---------------------------------------------------------------------------
# Core clustering behavior
# ---------------------------------------------------------------------------


def test_cluster_finds_well_separated_groups() -> None:
    """3 dense cones against a noise background -> 3 cluster outputs."""
    rng = np.random.default_rng(42)
    embeddings = _mixed_data(
        rng,
        cluster_sizes=[(15, 0.3), (15, 0.3), (15, 0.3)],
        noise_n=30,
    )

    clusterer = HdbscanClusterer(min_cluster_size=10)
    clusters = clusterer.cluster(embeddings)

    assert len(clusters) == 3
    assert all(c.size >= 10 for c in clusters)


def test_cluster_marks_small_groups_as_noise() -> None:
    """Clusters smaller than min_cluster_size are dropped by the post-filter.

    Setup: 2 dense 15-pt cones and 1 tiny 2-pt group. The 2-pt group
    will form its own density-connected cluster (HDBSCAN's noise label
    or a small label) that the post-fit size filter must drop because
    2 < min_cluster_size=10. The 15-pt cones survive.
    """
    rng = np.random.default_rng(42)
    embeddings = _mixed_data(
        rng,
        cluster_sizes=[(15, 0.1), (15, 0.1), (2, 0.1)],  # 2-pt group too small
        noise_n=30,
    )

    clusterer = HdbscanClusterer(min_cluster_size=10, min_samples=4)
    clusters = clusterer.cluster(embeddings)

    # The 2-pt group lives at indices 30..31. It must NOT appear in
    # any cluster's indices — the post-fit size filter dropped it.
    small_group = set(range(30, 32))
    for cl in clusters:
        assert small_group.isdisjoint(set(cl.indices)), (
            f"small group leaked into cluster of size {cl.size}: {cl.indices}"
        )

    # At least one cluster (the 15-pt cones + their noise) must come back.
    assert len(clusters) >= 1


def test_cluster_filters_oversized_clusters() -> None:
    """A 1500-pt "general question" cluster is dropped by max_cluster_size."""
    rng = np.random.default_rng(42)
    # 1500 moderately-spread points — would form one big cluster under
    # HDBSCAN's density definition, but exceeds max_cluster_size.
    big = _scattered_noise(rng, n=1500) + _unit_direction(rng) * 0.1
    big = big / np.linalg.norm(big, axis=1, keepdims=True)
    small_center = _unit_direction(rng)
    small = _tight_cone(rng, center=small_center, n=15, max_angle=0.3)
    # Add some background noise so HDBSCAN has a contrast to work with
    embeddings = np.vstack([big, small, _scattered_noise(rng, n=30)])

    clusterer = HdbscanClusterer(min_cluster_size=10, max_cluster_size=1000)
    clusters = clusterer.cluster(embeddings)

    # big is excluded (> max_cluster_size); small is included
    assert len(clusters) == 1
    assert clusters[0].size == 15


def test_cluster_centroid_is_nearest_to_centroid() -> None:
    """Cluster.centroid_idx should point to a dense point, not the outlier.

    Setup: 2 dense 15-pt cones + 1 "outlier" placed near (but not in)
    the first cone, against a noise background. The outlier must not
    be picked as the first cone's centroid.
    """
    rng = np.random.default_rng(42)
    cone_center = _unit_direction(rng)
    other_center = _unit_direction(rng)
    cone1 = _tight_cone(rng, center=cone_center, n=15, max_angle=0.1)
    cone2 = _tight_cone(rng, center=other_center, n=15, max_angle=0.1)
    # outlier: same direction as cone1, offset in first dim
    outlier = cone_center + np.full(_DIM, 0.3)
    outlier = outlier / np.linalg.norm(outlier)
    outlier = outlier.reshape(1, -1)
    embeddings = np.vstack(
        [cone1, cone2, outlier, _scattered_noise(rng, n=30)]
    )

    clusterer = HdbscanClusterer(min_cluster_size=10, min_samples=2)
    clusters = clusterer.cluster(embeddings)

    # The cluster that contains cone1 (idx 0..14) must pick a dense
    # point as centroid, NOT the outlier at idx 30.
    cone1_indices = set(range(15))
    cone1_cluster = next(
        c for c in clusters if cone1_indices & set(c.indices)
    )
    assert cone1_cluster.centroid_idx < 15, (
        f"outlier {cone1_cluster.centroid_idx} was picked as centroid; "
        f"expected a dense point from idx 0..14"
    )


def test_cluster_empty_input() -> None:
    embeddings = np.empty((0, _DIM))
    clusterer = HdbscanClusterer()
    assert clusterer.cluster(embeddings) == []


# ---------------------------------------------------------------------------
# L2-normalize + edge cases
# ---------------------------------------------------------------------------


def test_cluster_normalizes_vectors_internally() -> None:
    """Clusters with very different magnitudes still separate correctly.

    Proves the clusterer is doing L2-normalize internally: without it,
    the distance between cluster centers is dominated by magnitude, not
    direction. The two clusters here are 100x apart in magnitude but
    in distinct directions, plus a noise background. The clusterer
    must still find ≥ 1 cluster and the two groups must end up in
    separate clusters.
    """
    rng = np.random.default_rng(42)
    a_dir = _unit_direction(rng)
    b_dir = _unit_direction(rng)
    # Cluster A: radius ~1 (small)
    a = a_dir + rng.normal(0, 0.05, (12, _DIM))
    # Cluster B: radius ~100 (large)
    b = b_dir * 100 + rng.normal(0, 5, (12, _DIM))
    embeddings = np.vstack([a, b, _scattered_noise(rng, n=30)])

    clusterer = HdbscanClusterer(min_cluster_size=10)
    clusters = clusterer.cluster(embeddings)

    # The two distinct-direction groups must end up in ≥ 2 clusters.
    a_indices = set(range(12))
    b_indices = set(range(12, 24))
    cluster_has_a = [cl for cl in clusters if a_indices & set(cl.indices)]
    cluster_has_b = [cl for cl in clusters if b_indices & set(cl.indices)]
    assert cluster_has_a and cluster_has_b, (
        f"A and B must end up in separate clusters. "
        f"clusters: {[(cl.id, cl.indices) for cl in clusters]}"
    )
    # And the A cluster and the B cluster must be different.
    a_cluster_ids = {cl.id for cl in cluster_has_a}
    b_cluster_ids = {cl.id for cl in cluster_has_b}
    assert a_cluster_ids.isdisjoint(b_cluster_ids)


def test_cluster_handles_zero_vectors_gracefully() -> None:
    """Zero-norm rows are dropped, not NaN'd into the output.

    A row of all zeros cannot be L2-normalized (division by zero); the
    clusterer must drop it and continue. The valid points must still
    form ≥ 1 cluster that includes the dense cone, and the zero row
    must NOT appear in any returned ``Cluster.indices`` (those indices
    reference the caller's original array).
    """
    rng = np.random.default_rng(42)
    center = _unit_direction(rng)
    good = _tight_cone(rng, center=center, n=15, max_angle=0.1)
    zero = np.zeros((1, _DIM))
    embeddings = np.vstack([good, zero, _scattered_noise(rng, n=30)])

    clusterer = HdbscanClusterer(min_cluster_size=10)
    clusters = clusterer.cluster(embeddings)

    # The zero row is at idx 15 in the input. It must NOT appear in
    # any cluster's indices.
    for cl in clusters:
        assert 15 not in cl.indices

    # The dense cone (idx 0..14) must be picked up by ≥ 1 cluster
    # (it can be merged with nearby noise, so we don't assert the
    # cluster is exactly size 15 — just that the cone indices are
    # in some cluster together).
    cone_indices = set(range(15))
    picked_up = any(cone_indices.issubset(set(cl.indices)) for cl in clusters)
    cluster_summaries = [(cl.id, cl.size) for cl in clusters]
    assert picked_up, f"cone indices not in any cluster: {cluster_summaries}"


def test_cluster_single_point_returns_empty() -> None:
    """A single point cannot form a cluster of any size; return []."""
    rng = np.random.default_rng(42)
    embeddings = _unit_direction(rng).reshape(1, -1)
    clusterer = HdbscanClusterer(min_cluster_size=10)
    assert clusterer.cluster(embeddings) == []


def test_cluster_below_min_cluster_size_returns_empty() -> None:
    """Input smaller than min_cluster_size cannot form a cluster; return [].

    Regression guard: previously this fed into HDBSCAN directly which
    produced unpredictable noise assignments. Now the clusterer should
    short-circuit.
    """
    rng = np.random.default_rng(42)
    points = np.vstack([_unit_direction(rng) for _ in range(5)])
    clusterer = HdbscanClusterer(min_cluster_size=10)
    assert clusterer.cluster(points) == []


def test_cluster_all_identical_vectors() -> None:
    """20 identical non-zero vectors: clusterer must not crash.

    Locks current acceptable behavior: HDBSCAN's tiebreak on degenerate
    input is non-deterministic, but the clusterer must not raise. Either
    a single cluster of 20 (HDBSCAN treats them as a dense manifold) or
    all-noise (cosine distance 0 everywhere, no density gradient) is
    acceptable; both are valid for this degenerate case.
    """
    rng = np.random.default_rng(42)
    center = _unit_direction(rng)
    embeddings = np.tile(center, (20, 1))
    clusterer = HdbscanClusterer(min_cluster_size=10)
    clusters = clusterer.cluster(embeddings)
    assert clusters == [] or (
        len(clusters) == 1 and clusters[0].size == 20
    )
