"""The analysis grid: a regular raster over mainland Norway in EPSG:25833."""

import warnings
from dataclasses import dataclass

import numpy as np
from affine import Affine
from rasterio.windows import Window

from soppkart import config


@dataclass(frozen=True)
class Grid:
    x0: float
    y0: float
    res: int
    width: int
    height: int
    crs: str = config.CRS

    @classmethod
    def norway(cls, res: int = config.RESOLUTION) -> "Grid":
        if res % 16 != 0:
            raise ValueError(f"resolution must be a multiple of 16, got {res}")
        x0, y0 = config.GRID_ORIGIN
        width = config.GRID_SIZE_M[0] // res
        height = config.GRID_SIZE_M[1] // res
        return cls(x0=x0, y0=y0, res=res, width=width, height=height)

    @property
    def shape(self) -> tuple[int, int]:
        return (self.height, self.width)

    @property
    def transform(self) -> Affine:
        return Affine(self.res, 0.0, self.x0, 0.0, -self.res, self.y0)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (
            self.x0,
            self.y0 - self.height * self.res,
            self.x0 + self.width * self.res,
            self.y0,
        )

    def fine_factor(self, min_res: int) -> int:
        """Largest subdivision factor that keeps sub-cells at least min_res metres.

        Used for helper rasters (soil, land cover, water) so they stay detailed
        at coarse resolutions without running out of memory at fine ones.
        """
        return max(1, self.res // min_res)

    def fine(self, factor: int) -> "Grid":
        """Same extent at a finer resolution."""
        return Grid(self.x0, self.y0, self.res // factor, self.width * factor, self.height * factor)

    def subgrid(self, row: int, col: int, height: int, width: int) -> "Grid":
        """The part of this grid starting at (row, col) with the given size."""
        return Grid(self.x0 + col * self.res, self.y0 - row * self.res, self.res, width, height)

    def expand(self, cells: int) -> "Grid":
        """This grid with a margin of `cells` cells on every side."""
        return Grid(
            self.x0 - cells * self.res,
            self.y0 + cells * self.res,
            self.res,
            self.width + 2 * cells,
            self.height + 2 * cells,
        )

    def chunk_offsets(self, sub: "Grid") -> tuple[int, int, int, int]:
        """(row, col, height, width) of a subgrid within this grid."""
        col = round((sub.x0 - self.x0) / self.res)
        row = round((self.y0 - sub.y0) / self.res)
        return (row, col, sub.height, sub.width)

    def window_of(self, sub: "Grid") -> Window:
        row, col, height, width = self.chunk_offsets(sub)
        return Window(col, row, width, height)  # type: ignore[call-arg]

    def rowcol(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Row/col indices for coordinates; -1 where outside the grid."""
        col = np.floor((x - self.x0) / self.res).astype(np.int64)
        row = np.floor((self.y0 - y) / self.res).astype(np.int64)
        outside = (col < 0) | (col >= self.width) | (row < 0) | (row >= self.height)
        col[outside] = -1
        row[outside] = -1
        return row, col


def block_reduce_mean(arr: np.ndarray, factor: int) -> np.ndarray:
    """Average non-overlapping factor x factor blocks, ignoring NaN."""
    h, w = arr.shape
    blocks = arr.reshape(h // factor, factor, w // factor, factor)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmean(blocks, axis=(1, 3)).astype(np.float32)
