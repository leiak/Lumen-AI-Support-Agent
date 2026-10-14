"""HDBSCAN clustering over conversation embeddings.

HDBSCAN chosen over K-means because:
- K-means requires pre-specifying K (unknown for arbitrary support history)
- HDBSCAN naturally identifies noise points (small groups marked as -1)
- Hierarchical structure can reveal sub-clusters for KB granularity

Distance metric: L2-normalize each row, then euclidean on unit vectors.
For unit vectors, euclidean distance is ``sqrt(2 * (1 - cos(a, b)))`` —
a strict monotonic transform of cosine distance, so HDBSCAN's
clustering decisions are mathematically equivalent to using cosine
distance directly. We use euclidean (not a precomputed matrix) because:

- Memory: O(n * d) instead of O(n²). For the history-mining scale
  (low-thousands of customer messages per cron run), that's ~80 MB
  vs ~400 MB at 2048-dim.
- Compute: sklearn's BallTree with euclidean metric works in 2048-dim
  by effectively brute-forcing the k-NN search (curse of
  dimensionality), so the euclidean path is no slower than precomputed.
- Simplicity: one less dependency (no scipy ``pdist``), and the
  HDBSCAN call site stays a single ``fit_predict`` line.

We L2-normalize (rather than call HDBSCAN with ``metric='cosine'``)
because sklearn 1.x dropped ``metric='cosine'`` from BallTree — and
because the L2-normalize approach is robust to embedding models that
produce vectors with very different magnitudes (e.g. Doubao
``doubao-embedding-vision`` 2048-dim vs OpenAI ``text-embedding-3-small``
1536-dim). Magnitude is not semantic; only direction is.

``min_samples`` defaults to 1 (not 5) because normalized+euclidean
produces much tighter neighborhoods than raw euclidean. The original
``min_samples=5`` was tuned for raw euclidean on 1536-dim text-embedding
output and silently marked everything as noise under Doubao's
2048-dim vision embeddings — see git history. ``min_samples`` is
intentionally NOT a config setting; operators should tune
``min_cluster_size`` instead.
"""
from __future__ import annotations

from dataclasses import dataclass

import hdbscan
import numpy as np

# Below this L2 norm, a row is considered a zero vector and dropped from
# clustering. Prevents ``v / |v|`` from producing NaN/Inf and poisoning
# the BallTree search. Kept as a module constant (not a parameter)
# because the threshold is an internal numerical-stability guard, not
# a tuning knob.
_ZERO_NORM_EPS = 1e-12


@dataclass(frozen=True)
class Cluster:
    id: int
    centroid_idx: int  # Index in the caller's original embedding array
    indices: tuple[int, ...]  # All indices in this cluster (immutable)

    @property
    def size(self) -> int:
        return len(self.indices)


class HdbscanClusterer:
    def __init__(
        self,
        *,
        min_cluster_size: int = 10,
        min_samples: int = 1,
        max_cluster_size: int = 1000,
    ) -> None:
        """HDBSCAN config.

        min_cluster_size: smallest valid cluster (post-filter threshold).
        min_samples: HDBSCAN core-point parameter. Default 1 — see the
            module docstring for why; the old default of 5 was tuned for
            raw euclidean on 1536-dim text embeddings and silently
            marked everything as noise under Doubao's 2048-dim vision
            embeddings. Tune ``min_cluster_size`` instead.
        max_cluster_size: largest valid cluster (caps mega-clusters like
            "general question" that would dominate the KB).
        """
        self._min_size = min_cluster_size
        self._min_samples = min_samples
        self._max_size = max_cluster_size

    def cluster(self, embeddings: np.ndarray) -> list[Cluster]:
        """Cluster ``embeddings`` via HDBSCAN on the L2-normalized unit sphere.

        Returns a list of valid clusters (after size filtering). Each
        cluster's ``indices`` and ``centroid_idx`` reference the
        caller's ORIGINAL embedding array, not the post-filter array —
        the clusterer transparently drops zero-norm rows so the caller
        doesn't need to know about them.

        Returns ``[]`` for any of:
          - empty input
          - 1-point input (cannot form any cluster)
          - fewer than ``min_cluster_size`` non-zero rows (impossible)
          - HDBSCAN finding no surviving clusters after size filtering
        """
        if len(embeddings) == 0:
            return []

        # Single-point input: HDBSCAN's kdtree query would crash with
        # "k must be <= n_points" (k = min_samples). Short-circuit.
        if len(embeddings) == 1:
            return []

        # L2-normalize each row. Drop zero-norm rows up front (they
        # would NaN the BallTree). Track the mapping from
        # post-filter row index -> caller's original index so the
        # returned ``Cluster.indices`` still align with the caller's
        # array.
        norms = np.linalg.norm(embeddings, axis=1)
        keep_mask = norms > _ZERO_NORM_EPS
        if not keep_mask.any():
            return []

        # If we kept fewer than min_cluster_size points, no cluster can
        # possibly form — short-circuit before HDBSCAN sees a degenerate
        # input. (HDBSCAN's behavior on tiny inputs is non-deterministic
        # and not worth relying on.)
        if int(keep_mask.sum()) < self._min_size:
            return []

        original_indices = np.where(keep_mask)[0]
        normed = embeddings[keep_mask] / norms[keep_mask, None]

        labels = hdbscan.HDBSCAN(
            min_cluster_size=self._min_size,
            min_samples=self._min_samples,
            metric="euclidean",  # euclidean on unit vectors = sqrt(2(1-cos))
        ).fit_predict(normed)

        clusters: list[Cluster] = []
        for label in set(labels):
            if label == -1:
                continue  # noise — not a valid cluster
            local_indices = list(np.where(labels == label)[0])
            if len(local_indices) < self._min_size or len(local_indices) > self._max_size:
                continue
            # Centroid: the original point closest to the cluster mean.
            # Operate in normalized space — argmin is invariant under the
            # sqrt(2(1-cos)) monotonic transform, so picking the
            # nearest-to-mean point in normalized space is the same
            # pick as in cosine space. We index into the original
            # ``embeddings`` via the original_indices map so the caller
            # sees indices into their own array.
            cluster_normed = normed[local_indices]
            mean = cluster_normed.mean(axis=0)
            mean /= np.linalg.norm(mean)  # keep mean on the unit sphere
            distances = np.linalg.norm(cluster_normed - mean, axis=1)
            local_centroid_idx = int(np.argmin(distances))
            global_centroid_idx = int(original_indices[local_indices[local_centroid_idx]])
            global_indices = tuple(int(original_indices[i]) for i in local_indices)
            clusters.append(
                Cluster(
                    id=int(label),
                    centroid_idx=global_centroid_idx,
                    indices=global_indices,
                )
            )
        return clusters
