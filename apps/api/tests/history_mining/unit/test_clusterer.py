"""Unit tests for HdbscanClusterer (Stage 18 / Task 7).

Tests cluster formation, noise filtering, max-size filtering,
centroid selection, and empty input handling. HDBSCAN behavior is
deterministic given the input data + a seeded numpy generator, so we
use a fixed seed (``rng = np.random.default_rng(42)``) so that the
test outcomes are stable across runs.
"""
from __future__ import annotations

import numpy as np

from history_mining.clusterer import Cluster, HdbscanClusterer


def test_cluster_finds_well_separated_groups() -> None:
    """3 clear clusters in 2D -> 3 cluster outputs."""
    rng = np.random.default_rng(42)  # deterministic for test stability
    c1 = rng.normal(0, 0.1, (15, 2)) + np.array([0, 0])
    c2 = rng.normal(0, 0.1, (15, 2)) + np.array([5, 5])
    c3 = rng.normal(0, 0.1, (15, 2)) + np.array([10, 0])
    embeddings = np.vstack([c1, c2, c3])

    clusterer = HdbscanClusterer(min_cluster_size=10, min_samples=5)
    clusters = clusterer.cluster(embeddings)

    assert len(clusters) == 3
    assert all(c.size >= 10 for c in clusters)


def test_cluster_marks_small_groups_as_noise() -> None:
    """Groups smaller than min_cluster_size -> marked as noise (not in output)."""
    rng = np.random.default_rng(42)
    # Two well-separated large clusters + a small group (3 points) far away
    c1 = rng.normal(0, 0.1, (15, 2)) + np.array([0, 0])
    c2 = rng.normal(0, 0.1, (15, 2)) + np.array([5, 5])
    c_small = rng.normal(0, 0.1, (3, 2)) + np.array([10, 0])  # too small
    embeddings = np.vstack([c1, c2, c_small])

    # min_samples is HDBSCAN's core-point parameter; the 3-point group is
    # actually excluded by the post-fit size filter (len < min_cluster_size).
    clusterer = HdbscanClusterer(min_cluster_size=10, min_samples=4)
    clusters = clusterer.cluster(embeddings)

    # Only the two large clusters returned; the small group is filtered
    assert len(clusters) == 2
    assert all(c.size == 15 for c in clusters)


def test_cluster_filters_oversized_clusters() -> None:
    """Clusters larger than max_cluster_size -> discarded."""
    rng = np.random.default_rng(42)
    big = rng.normal(0, 0.1, (1500, 2))
    small = rng.normal(0, 0.1, (15, 2)) + np.array([5, 5])
    embeddings = np.vstack([big, small])

    clusterer = HdbscanClusterer(min_cluster_size=10, max_cluster_size=1000)
    clusters = clusterer.cluster(embeddings)

    # big is excluded, small is included
    assert len(clusters) == 1
    assert clusters[0].size == 15


def test_cluster_centroid_is_nearest_to_centroid() -> None:
    """Cluster.centroid_idx should point to the closest point to mean.

    Setup: two well-separated dense clusters. Cluster 0 also contains
    one additional point (idx 30) that's noticeably farther from the
    cluster mean. The centroid should be one of the dense points
    (idx 0..14), not the outlying member at idx 30.
    """
    rng = np.random.default_rng(42)
    c1 = rng.normal(0, 0.1, (15, 2)) + np.array([0, 0])
    c2 = rng.normal(0, 0.1, (15, 2)) + np.array([5, 5])
    far_member = np.array([[0.5, 0.5]])  # part of cluster 0 but distant
    embeddings = np.vstack([c1, c2, far_member])

    clusterer = HdbscanClusterer(min_cluster_size=10, min_samples=2)
    clusters = clusterer.cluster(embeddings)

    assert len(clusters) == 2
    # Pick the cluster that contains idx 30 (the far_member)
    cluster_with_far = next(c for c in clusters if 30 in c.indices)
    centroid_idx = cluster_with_far.centroid_idx
    assert centroid_idx < 15  # dense point, not the far one


def test_cluster_empty_input() -> None:
    embeddings = np.empty((0, 2))
    clusterer = HdbscanClusterer()
    assert clusterer.cluster(embeddings) == []