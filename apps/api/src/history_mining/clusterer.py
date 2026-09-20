"""HDBSCAN clustering over conversation embeddings.

HDBSCAN chosen over K-means because:
- K-means requires pre-specifying K (unknown for arbitrary support history)
- HDBSCAN naturally identifies noise points (small groups marked as -1)
- Hierarchical structure can reveal sub-clusters for KB granularity
"""
from __future__ import annotations

from dataclasses import dataclass

import hdbscan
import numpy as np


@dataclass(frozen=True)
class Cluster:
    id: int
    centroid_idx: int  # Index in original embeddings array closest to mean
    indices: tuple[int, ...]  # All indices in this cluster (immutable)

    @property
    def size(self) -> int:
        return len(self.indices)


class HdbscanClusterer:
    def __init__(
        self,
        *,
        min_cluster_size: int = 10,
        min_samples: int = 5,
        max_cluster_size: int = 1000,
    ) -> None:
        """HDBSCAN config.

        min_cluster_size: smallest valid cluster (post-filter threshold).
        min_samples: HDBSCAN core-point parameter; conservative → fewer core points.
        max_cluster_size: largest valid cluster (caps mega-clusters like "general
            question" that would dominate the KB).
        """
        self._min_size = min_cluster_size
        self._min_samples = min_samples
        self._max_size = max_cluster_size

    def cluster(self, embeddings: np.ndarray) -> list[Cluster]:
        """Returns list of valid clusters (after size filtering).

        HDBSCAN labels: -1 means noise (filtered out), >=0 are cluster ids.
        """
        if len(embeddings) == 0:
            return []

        hdb = hdbscan.HDBSCAN(
            min_cluster_size=self._min_size,
            min_samples=self._min_samples,
        )
        labels = hdb.fit_predict(embeddings)

        clusters: list[Cluster] = []
        for label in set(labels):
            if label == -1:
                continue  # noise — not a valid cluster
            indices = list(np.where(labels == label)[0])
            if len(indices) < self._min_size or len(indices) > self._max_size:
                continue
            cluster_embeddings = embeddings[indices]
            mean = cluster_embeddings.mean(axis=0)
            # Centroid: the original point closest to the mean
            distances = np.linalg.norm(cluster_embeddings - mean, axis=1)
            local_centroid_idx = int(np.argmin(distances))
            global_centroid_idx = int(indices[local_centroid_idx])
            clusters.append(
                Cluster(
                    id=int(label),
                    centroid_idx=global_centroid_idx,
                    indices=tuple(indices),
                )
            )
        return clusters