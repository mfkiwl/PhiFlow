from numbers import Number
from typing import Tuple, Union

from phi import math
from phi.math import Tensor, Shape
from . import BaseBox, Box
from ._geom import Geometry
from ._sphere import Sphere
from phiml.math._shape import parse_dim_order


class _EmbeddedGeometry(Geometry):

    def __init__(self, geometry, axes: Tuple[str]):
        self.geometry = geometry
        self.axes = axes  # spatial axis order

    @property
    def spatial_rank(self) -> int:
        return len(self.axes)

    @property
    def center(self) -> Tensor:
        raise NotImplementedError()
        # c = self.geometry.center.vector.unstack()
        # return math.stack()

    @property
    def shape(self) -> Shape:
        return self.geometry.shape.with_dim_size('vector', self.axes)

    @property
    def volume(self) -> Tensor:
        raise NotImplementedError()

    def unstack(self, dimension: str) -> tuple:
        raise NotImplementedError()

    def _down_project(self, location: Tensor):
        item_names = list(location.shape.get_item_names('vector'))
        for dim in self.axes:
            if dim not in self.geometry.shape.get_item_names('vector'):
                item_names.remove(dim)
        projected_loc = location.vector[item_names]
        return projected_loc

    def lies_inside(self, location: Tensor) -> Tensor:
        return self.geometry.lies_inside(self._down_project(location))

    def approximate_signed_distance(self, location: Tensor) -> Tensor:
        return self.geometry.approximate_signed_distance(self._down_project(location))

    def push(self, positions: Tensor, outward: bool = True, shift_amount: float = 0) -> Tensor:
        raise NotImplementedError()

    def sample_uniform(self, *shape: math.Shape) -> Tensor:
        raise NotImplementedError()

    def bounding_radius(self) -> Tensor:
        raise NotImplementedError()

    def bounding_half_extent(self) -> Tensor:
        raise NotImplementedError()

    def shifted(self, delta: Tensor) -> 'Geometry':
        raise NotImplementedError()

    def at(self, center: Tensor) -> 'Geometry':
        raise NotImplementedError()

    def rotated(self, angle: Union[float, Tensor]) -> 'Geometry':
        raise NotImplementedError()

    def scaled(self, factor: Union[float, Tensor]) -> 'Geometry':
        raise NotImplementedError()

    def __hash__(self):
        return hash(self.geometry) + hash(self.axes)


def embed(geometry: Geometry, projected_dims: Union[math.Shape, str, tuple, list, None]) -> Geometry:
    """
    Adds fake spatial dimensions to a geometry.
    The geometry value will be constant along the added dimensions, as if it had infinite length in these directions.

    Dimensions that are already present with `geometry` are ignored.

    Args:
        geometry: `Geometry`
        projected_dims: Additional dimensions

    Returns:
        `Geometry` with spatial rank `geometry.spatial_rank + projected_dims.rank`.
    """
    if projected_dims is None:
        return geometry
    axes = parse_dim_order(projected_dims)
    embedded_axes = [a for a in axes if a not in geometry.shape.get_item_names('vector')]
    if not embedded_axes:
        return geometry
    for name in reversed(geometry.shape.get_item_names('vector')):
        if name not in projected_dims:
            axes = (name,) + axes
    if isinstance(geometry, BaseBox):
        box = geometry.corner_representation()
        return box * Box(**{dim: None for dim in embedded_axes})
    return _EmbeddedGeometry(geometry, axes)


def infinite_cylinder(center=None, radius=None, inf_dim: Union[str, Shape, tuple, list] = None, **center_) -> Geometry:
    """
    Creates an infinite cylinder.
    This is equal to embedding an `n`-dimensional `Sphere` in `n+1` dimensions.

    See Also:
        `Sphere`, `embed`

    Args:
        center: Center coordinates without `inf_dim`. Alternatively use keyword arguments.
        radius: Cylinder radius.
        inf_dim: Dimension along which the cylinder is infinite.
            Use `Geometry.rotated()` if the direction does not align with an axis.
        **center_: Alternatively specify center coordinates without `inf_dim` as keyword arguments.

    Returns:
        `Geometry`
    """
    sphere = Sphere(center, radius, **center_)
    return embed(sphere, inf_dim)
