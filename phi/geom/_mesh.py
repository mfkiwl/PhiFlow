import os
import warnings
from dataclasses import dataclass
from functools import cached_property
from numbers import Number
from typing import Dict, List, Sequence, Union, Any, Tuple, Optional

import numpy as np
from scipy.sparse import csr_matrix, coo_matrix

from phiml.math import to_format, is_sparse, non_channel, non_batch, batch, pack_dims, unstack, tensor, si2d, non_dual, nonzero, stored_indices, stored_values, scatter, \
    find_closest, sqrt, where, vec_normalize, argmax, broadcast, to_int32, cross_product, zeros, random_normal, EMPTY_SHAPE, meshgrid, mean, reshaped_numpy, range_tensor, convolve, \
    assert_close, shift, pad, extrapolation, NUMPY, sum as sum_, with_diagonal, flatten, ones_like, dim_mask
from phiml.math._magic_ops import getitem_dataclass
from phiml.math._sparse import CompactSparseTensor
from phiml.math.extrapolation import as_extrapolation, PERIODIC
from phiml.math.magic import slicing_dict
from . import bounding_box
from ._functions import plane_sgn_dist
from ._geom import Geometry, Point, scale, NoGeometry
from ._box import Box, BaseBox
from ._graph import Graph, graph
from ..math import Tensor, Shape, channel, shape, instance, dual, rename_dims, expand, spatial, wrap, sparse_tensor, stack, vec_length, tensor_like, \
    pairwise_distances, concat, Extrapolation


class _MeshType(type):
    """Metaclass containing the user-friendly (legacy) Mesh constructor."""
    def __call__(cls,
                 vertices: Union[Geometry, Tensor],
                 elements: Tensor,
                 element_rank: int,
                 boundaries: Dict[str, Dict[str, slice]],
                 max_cell_walk: int = None,
                 variables=('vertices',),
                 values=()):
        if spatial(elements):
            assert elements.dtype.kind == int, f"elements listing vertices must be integer lists but got dtype {elements.dtype}"
        else:
            assert elements.dtype.kind == bool, f"element matrices must be of type bool but got {elements.dtype}"
        if not isinstance(vertices, Geometry):
            vertices = Point(vertices)
        if max_cell_walk is None:
            max_cell_walk = 2 if instance(elements).volume > 1 else 1
        result = cls.__new__(cls, vertices, elements, element_rank, boundaries, max_cell_walk, variables, values)
        result.__init__(vertices, elements, element_rank, boundaries, max_cell_walk, variables, values)  # also calls __post_init__()
        return result


@dataclass(frozen=True)
class Mesh(Geometry, metaclass=_MeshType):
    """
    Unstructured mesh, consisting of vertices and elements.
    
    Use `phi.geom.mesh()` or `phi.geom.mesh_from_numpy()` to construct a mesh manually or `phi.geom.load_su2()` to load one from a file.
    """

    vertices: Geometry
    """ Vertices are represented by a `Geometry` instance with an instance dim. """
    elements: Tensor
    """ elements: Sparse `Tensor` listing ordered vertex indices per element (solid or surface element, depending on `element_rank`).
    Must have one instance dim listing the elements and the corresponding dual dim to `vertices`.
    The vertex count of an element is equal to the number of elements in that row (i.e. summing the dual dim). """
    element_rank: int
    """The spatial rank of the elements. Solid elements have the same as the ambient space, faces one less."""
    boundaries: Dict[str, Dict[str, slice]]
    """Slices to retrieve boundary face values."""
    periodic: Sequence[str]
    """List of axis names that are periodic. Periodic boundaries must be named as axis- and axis+. For example `['x']` will connect the boundaries x- and x+."""
    face_format: str = 'csc'
    """Sparse matrix format for storing quantities that depend on a pair of neighboring elements, e.g. `face_area`, `face_normal`, `face_center`."""
    max_cell_walk: int = None

    variable_attrs: Tuple[str, ...] = ('vertices',)
    value_attrs: Tuple[str, ...] = ()

    @cached_property
    def shape(self) -> Shape:
        return non_dual(self.elements) & channel(self.vertices) & batch(self.vertices)

    @cached_property
    def cell_count(self):
        return instance(self.elements).size

    @cached_property
    def center(self) -> Tensor:
        if self.element_rank == self.spatial_rank:  # Compute volumetric center from faces
            return sum_(self.face_centers * self.face_areas, dual) / sum_(self.face_areas, dual)
        else:  # approximate center from vertices
            return self._vertex_mean

    @cached_property
    def _vertex_mean(self):
        """Mean vertex location per element."""
        vertex_count = sum_(self.elements, instance(self.vertices).as_dual())
        return (self.elements @ self.vertices.center) / vertex_count

    @cached_property
    def face_centers(self) -> Tensor:
        return self._faces['center']

    @property
    def face_areas(self) -> Tensor:
        return self._faces['area']

    @cached_property
    def face_normals(self) -> Tensor:
        if self.element_rank == self.spatial_rank:  # this cannot depend on element centers because that depends on the normals.
            normals = self._faces['normal']
            face_centers = self._faces['center']
            normals_out = normals.vector * (face_centers - self._vertex_mean).vector > 0
            normals = where(normals_out, normals, -normals)
            return normals
        raise NotImplementedError

    @cached_property
    def _faces(self) -> Dict[str, Tensor]:
        if self.element_rank == 2:
            centers, normals, areas, boundary_slices, vertex_connectivity = build_faces_2d(self.vertices.center, self.elements, self.boundaries, self.periodic, self._vertex_mean, self.face_format)
            return {
                'center': centers,
                'normal': normals,
                'area': areas,
                'boundary_slices': boundary_slices,
                'vertex_connectivity': vertex_connectivity,
            }
        return None

    @property
    def face_shape(self) -> Shape:
        return instance(self.elements) & dual

    @property
    def sets(self):
        return {
            'center': non_batch(self)-'vector',
            'vertex': instance(self.vertices),
            '~vertex': dual(self.elements)
        }

    def get_points(self, set_key: str) -> Tensor:
        if set_key == 'vertex':
            return self.vertices.center
        elif set_key == '~vertex':
            return si2d(self.vertices.center)
        else:
            return Geometry.get_points(self, set_key)

    def get_boundary(self, set_key: str) -> Dict[str, Dict[str, slice]]:
        if set_key in ['vertex', '~vertex']:
            return {}
        return Geometry.get_boundary(self, set_key)

    @property
    def boundary_elements(self) -> Dict[str, Dict[str, slice]]:
        return {}

    @property
    def boundary_faces(self) -> Dict[str, Dict[str, slice]]:
        return self._faces['boundary_slices']

    @property
    def all_boundary_faces(self) -> Dict[str, slice]:
        return {self.face_shape.dual.name: slice(instance(self).volume, None)}
    
    @property
    def interior_faces(self) -> Dict[str, slice]:
        return {self.face_shape.dual.name: slice(0, instance(self).volume)}

    def pad_boundary(self, value: Tensor, widths: Dict[str, Dict[str, slice]] = None, mode: Extrapolation or Tensor or Number = 0, **kwargs) -> Tensor:
        mode = as_extrapolation(mode)
        if self.face_shape.dual.name not in value.shape:
            value = rename_dims(value, instance, self.face_shape.dual)
        else:
            raise NotImplementedError
        if widths is None:
            widths = self.boundary_faces
        if isinstance(widths, (tuple, list)):
            if len(widths) == 0 or isinstance(widths[0], dict):  # add sliced-off slices
                pass
        dim = next(iter(next(iter(widths.values()))))
        slices = [slice(0, value.shape.get_size(dim))]
        values = [value]
        connectivity = self.connectivity
        for name, b_slice in widths.items():
            if b_slice[dim].stop - b_slice[dim].start > 0:
                slices.append(b_slice[dim])
                values.append(mode.sparse_pad_values(value, connectivity[b_slice], name, mesh=self, **kwargs))
        perm = np.argsort([s.start for s in slices])
        ordered_pieces = [values[i] for i in perm]
        return concat(ordered_pieces, dim, expand_values=True)

    @cached_property
    def cell_connectivity(self) -> Tensor:
        """
        Returns a bool-like matrix whose non-zero entries denote connected elements.
        In meshes or grids, elements are connected if they share a face in 3D, an edge in 2D, or a vertex in 1D.

        Returns:
            `Tensor` of shape (elements, ~elements)
        """
        return self.connectivity[self.interior_faces]

    @cached_property
    def boundary_connectivity(self) -> Tensor:
        return self.connectivity[self.all_boundary_faces]

    @cached_property
    def distance_matrix(self):
        return vec_length(pairwise_distances(self.center, edges=self.cell_connectivity, format='as edges', default=None))

    def faces_to_vertices(self, values: Tensor, reduce=sum):
        v = stored_values(values, invalid='keep')  # ToDo replace this once PhiML has support for dense instance dims and sparse scatter
        i = stored_values(self.face_vertices, invalid='keep')
        i = rename_dims(i, channel, instance)
        out_shape = non_channel(self.vertices) & shape(values).without(self.face_shape)
        return scatter(out_shape, i, v, mode=reduce, outside_handling='undefined')

    @cached_property
    def _cell_deltas(self):
        bounds = bounding_box(self.vertices)
        is_periodic = dim_mask(self.vector.item_names, self.periodic)
        return pairwise_distances(self.center, format=self.cell_connectivity, periodic=is_periodic, domain=(bounds.lower, bounds.upper))

    @cached_property
    def relative_face_distance(self):
        """|face_center - center| / |neighbor_center - center|"""
        cell_distances = vec_length(self._cell_deltas)
        assert (cell_distances > 0).all, f"All cells must have distance > 0 but found 0 distance at {nonzero(cell_distances == 0)}"
        face_distances = vec_length(self.face_centers[self.interior_faces] - self.center)
        return concat([face_distances / cell_distances, self.boundary_connectivity], self.face_shape.dual)

    @cached_property
    def neighbor_offsets(self):
        """Returns shift vector to neighbor centroids and boundary faces."""
        boundary_deltas = (self.face_centers - self.center)[self.all_boundary_faces]
        assert (vec_length(boundary_deltas) > 0).all, f"All boundary faces must be separated from the cell centers but 0 distance at the following {channel(stored_indices(boundary_deltas)).item_names[0]}:\n{nonzero(vec_length(boundary_deltas) == 0):full}"
        return concat([self._cell_deltas, boundary_deltas], self.face_shape.dual)

    @cached_property
    def neighbor_distances(self):
        return vec_length(self.neighbor_offsets)

    @property
    def faces(self) -> 'Geometry':
        """
        Assembles information about the boundaries of the elements that make up the surface.
        For 2D elements, the faces are edges, for 3D elements, the faces are planar elements.

        Returns:
            center: Center of face connecting a pair of elements. Shape (~elements, elements, vector).
                Returns 0-vectors for unconnected elements.
            area: Area of face connecting a pair of elements. Shape (~elements, elements).
                Returns 0 for unconnected elements.
            normal: Normal vector of face connecting a pair of elements. Shape (~elements, elements, vector).
                Unconnected elements are assigned the vector 0.
                The vector points out of polygon and into ~polygon.
        """
        return Point(self.face_centers)

    @property
    def connectivity(self) -> Tensor:
        return self.element_connectivity

    @cached_property
    def element_connectivity(self) -> Tensor:
        if self.element_rank == self.spatial_rank:
            if is_sparse(self.face_areas):
                return tensor_like(self.face_areas, True)
            else:
                return self.face_areas > 0
        else:  # fallback with no boundaries
            coo = to_format(self.elements, 'coo').numpy()
            connected_elements = coo @ coo.T
            connected_elements.data = np.ones_like(connected_elements.data)
            element_connectivity = wrap(connected_elements, instance(self.elements), instance(self.elements).as_dual())
            return element_connectivity

    @cached_property
    def vertex_connectivity(self) -> Tensor:
        if isinstance(self.vertices, Graph):
            return self.vertices.connectivity
        if self.element_rank == self.spatial_rank:
            return self._faces['vertex_connectivity']
        elif self.element_rank <= 2:
            coo = to_format(self.elements, 'coo').numpy()
            connected_points = coo.T @ coo  # ToDo this also counts vertices not connected by a single line/face as long as they are part of the same element
            if not np.all(connected_points.sum_(axis=1) > 0):
                warnings.warn("some vertices have no element connection at all", RuntimeWarning)
            connected_points.data = np.ones_like(connected_points.data)
            vertex_connectivity = wrap(connected_points, instance(self.vertices), dual(self.elements))
            return vertex_connectivity
        raise NotImplementedError

    @property
    def vertex_graph(self) -> Graph:
        if isinstance(self.vertices, Graph):
            return self.vertices
        assert self._vertex_connectivity is not None, f"vertex_graph not available because vertex_connectivity has not been computed"
        return graph(self.vertices, self._vertex_connectivity)

    def filter_unused_vertices(self) -> 'Mesh':
        coo = to_format(self.elements, 'coo').numpy()
        has_element = np.asarray(coo.sum_(0) > 0)[0]
        new_index = np.cumsum_(has_element) - 1
        new_index_t = wrap(new_index, dual(self.elements))
        has_element = wrap(has_element, instance(self.vertices))
        has_element_d = si2d(has_element)
        vertices = self.vertices[has_element]
        v_normals = self._vertex_normals[has_element_d]
        vertex_connectivity = None
        if self._vertex_connectivity is not None:
            vertex_connectivity = stored_indices(self._vertex_connectivity).index.as_batch()
            vertex_connectivity = new_index_t[{dual: vertex_connectivity}].index.as_channel()
            vertex_connectivity = sparse_tensor(vertex_connectivity, stored_values(self._vertex_connectivity), non_batch(self._vertex_connectivity).with_sizes(instance(vertices).size), False)
        if isinstance(self.elements, CompactSparseTensor):
            indices = new_index_t[{dual: self.elements._indices}]
            elements = CompactSparseTensor(indices, self.elements._values, self.elements._compressed_dims.with_size(instance(vertices).volume), self.elements._indices_constant, self.elements._matrix_rank)
        else:
            filtered_coo = coo_matrix((coo.data, (coo.row, new_index)), shape=(instance(self.elements).volume, instance(vertices).volume))  # ToDo keep sparse format
            elements = wrap(filtered_coo, self.elements.shape.without_sizes())
        return Mesh(vertices, elements, self.element_rank, self.boundaries, self._center, self._volume, self._normals, self.face_centers, self.face_normals, self.face_areas, None, v_normals, vertex_connectivity, self._element_connectivity, self._max_cell_walk)

    @property
    def volume(self) -> Tensor:
        if isinstance(self.elements, CompactSparseTensor) and self.element_rank == 2:
            if instance(self.vertices).volume > 0:
                A, B, C, *_ = unstack(self.vertices.center[self.elements._indices], dual)
                cross_area = vec_length(cross_product(B - A, C - A))
                fac = {3: 0.5, 4: 1}[dual(self.elements._indices).size]  # tri, quad, ...
                return fac * cross_area
            else:
                return zeros(instance(self.vertices))  # empty mesh
        elif self.element_rank == self.spatial_rank:
            vol_contributions = (self.face_centers.vector @ self.face_normals.vector) * self.face_areas
            return sum_(vol_contributions, dual) / self.spatial_rank
        raise NotImplementedError


    @property
    def normals(self) -> Tensor:
        """Extrinsic element normal space. This is a 0D vector for solid elements and 1D for surface elements."""
        if isinstance(self.elements, CompactSparseTensor) and self.element_rank == 2:
            corners = self.vertices[self.elements._indices]
            assert dual(corners).size == 3, f"signed distance currently only supports triangles"
            v1, v2, v3 = unstack(corners, dual)
            return vec_normalize(cross_product(v2 - v1, v3 - v1))
        raise NotImplementedError

    @property
    def vertex_normals(self) -> Tensor:
        v_normals = mean(self.elements * self.normals, instance)  # (~vertices,vector)
        return vec_normalize(v_normals)

    @property
    def vertex_positions(self) -> Tensor:
        """Lists the vertex centers along the corresponding dual dim to `self.vertices.center`."""
        return si2d(self.vertices.center)

    def lies_inside(self, location: Tensor) -> Tensor:
        idx = find_closest(self._center, location)
        for i in range(self._max_cell_walk):
            idx, leaves_mesh, is_outside, *_ = self.cell_walk_towards(location, idx, allow_exit=i == self._max_cell_walk - 1)
        return ~(leaves_mesh & is_outside)

    def approximate_signed_distance(self, location: Union[Tensor, tuple]) -> Tensor:
        if self.element_rank == 2 and self.spatial_rank == 3:
            closest_elem = find_closest(self._center, location)
            center = self._center[closest_elem]
            normal = self._normals[closest_elem]
            return plane_sgn_dist(center, normal, location)
        if self._center is None:
            raise NotImplementedError("Mesh.approximate_signed_distance only available when faces are built.")
        idx = find_closest(self._center, location)
        for i in range(self._max_cell_walk):
            idx, leaves_mesh, is_outside, distances, nb_idx = self.cell_walk_towards(location, idx, allow_exit=False)
        return max(distances, dual)

    def approximate_closest_surface(self, location: Tensor) -> Tuple[Tensor, Tensor, Tensor, Tensor, Tensor]:
        if self.element_rank == 2 and self.spatial_rank == 3:
            closest_elem = find_closest(self._center, location)
            center = self._center[closest_elem]
            normal = self._normals[closest_elem]
            face_size = sqrt(self._volume) * 4
            size = face_size[closest_elem]
            sgn_dist = plane_sgn_dist(center, normal, location)
            delta = center - location  # this is not accurate...
            outward = where(abs(sgn_dist) < size, normal, vec_normalize(delta))
            return sgn_dist, delta, outward, None, closest_elem
        # idx = find_closest(self._center, location)
        # for i in range(self._max_cell_walk):
        #     idx, leaves_mesh, is_outside, distances, nb_idx = self.cell_walk_towards(location, idx, allow_exit=False)
        # sgn_dist = max(distances, dual)
        # cell_normals = self.face_normals[idx]
        # normal = cell_normals[{dual: nb_idx}]
        # return sgn_dist, delta, normal, offset, face_index
        raise NotImplementedError

    def cell_walk_towards(self, location: Tensor, start_cell_idx: Tensor, allow_exit=False):
        """
        If `location` is not within the cell at index `from_cell_idx`, moves to a closer neighbor cell.

        Args:
            location: Target location as `Tensor`.
            start_cell_idx: Index of starting cell. Must be a valid cell index.
            allow_exit: If `True`, returns an invalid index for points outside the mesh, otherwise keeps the current index.

        Returns:
            index: Index of the neighbor cell or starting cell.
            leaves_mesh: Whether the walk crossed the mesh boundary. Then `index` is invalid. This is only possible if `allow_exit` is true.
            is_outside: Whether `location` was outside the cell at index `start_cell_idx`.
        """
        closest_normals = self.face_normals[start_cell_idx]
        closest_face_centers = self.face_centers[start_cell_idx]
        offsets = closest_normals.vector @ closest_face_centers.vector  # this dot product could be cashed in the mesh
        distances = closest_normals.vector @ location.vector - offsets
        is_outside = any(distances > 0, dual)
        nb_idx = argmax(distances, dual).index[0]  # cell index or boundary face index
        leaves_mesh = nb_idx >= instance(self).volume
        next_idx = where(is_outside & (~leaves_mesh | allow_exit), nb_idx, start_cell_idx)
        return next_idx, leaves_mesh, is_outside, distances, nb_idx

    def sample_uniform(self, *shape: Shape) -> Tensor:
        raise NotImplementedError

    def bounding_radius(self) -> Tensor:
        center = self.elements * self.center
        vert_pos = rename_dims(self.vertices.center, instance, dual)
        dist_to_vert = vec_length(vert_pos - center)
        max_dist = max(dist_to_vert, dual)
        return max_dist

    def bounding_half_extent(self) -> Tensor:
        center = self.elements * self.center
        vert_pos = rename_dims(self.vertices.center, instance, dual)
        max_delta = max(abs(vert_pos - center), dual)
        return max_delta

    def bounding_box(self) -> 'BaseBox':
        return self.vertices.bounding_box()

    @property
    def bounds(self):
        return Box(min(self.vertices.center, instance), max(self.vertices.center, instance))

    def at(self, center: Tensor) -> 'Mesh':
        if instance(self.elements) in center.shape:
            raise NotImplementedError("Setting Mesh positions only supported for vertices, not elements")
        if dual(self.elements) in center.shape:
            center = rename_dims(center, dual, instance(self.vertices))
        if instance(self.vertices) in center.shape:
            vertices = self.vertices.at(center)
            return mesh(vertices, self.elements, self.boundaries)
        else:
            shift = center - self.bounds.center
            return self.shifted(shift)

    def shifted(self, delta: Tensor) -> 'Mesh':
        if instance(self.elements) in delta.shape:
            raise NotImplementedError("Shifting Mesh positions only supported for vertices, not elements")
        if dual(self.elements) in delta.shape:
            delta = rename_dims(delta, dual, instance(self.vertices))
        if instance(self.vertices) in delta.shape:
            vertices = self.vertices.shifted(delta)
            return mesh(vertices, self.elements, self.boundaries)
        else:  # shift everything
            # ToDo transfer cached properties
            vertices = self.vertices.shifted(delta)
            center = self._center + delta
            return Mesh(vertices, self.elements, self.element_rank, self.boundaries, center, self._volume, self._normals, self.face_centers, self.face_normals, self.face_areas, self.face_vertices, self._vertex_normals, self._vertex_connectivity, self._element_connectivity, self._max_cell_walk)

    def rotated(self, angle: Union[float, Tensor]) -> 'Geometry':
        raise NotImplementedError

    def scaled(self, factor: float | Tensor) -> 'Geometry':
        pivot = self.bounds.center
        vertices = scale(self.vertices, factor, pivot)
        center = scale(Point(self._center), factor, pivot).center
        volume = self._volume * factor**self.element_rank if self._volume is not None else None
        face_areas = None
        return Mesh(vertices, self.elements, self.element_rank, self.boundaries, center, volume, self._normals, self.face_centers, self.face_normals, face_areas, self.face_vertices, self._vertex_normals, self._vertex_connectivity, self._element_connectivity, self._max_cell_walk)

    def __getitem__(self, item):
        item: dict = slicing_dict(self, item)
        assert not spatial(self.elements).only(tuple(item)), f"Cannot slice vertex lists ('{spatial(self.elements)}') but got slicing dict {item}"
        assert not instance(self.vertices).only(tuple(item)), f"Slicing by vertex indices ('{instance(self.vertices)}') not supported but got slicing dict {item}"
        return getitem_dataclass(self, item, keepdims=[self.shape.instance.name, 'vector'])

    def __repr__(self):
        return Geometry.__repr__(self)


@broadcast
def load_su2(file_or_mesh: str, cell_dim=instance('cells'), face_format: str = 'csc') -> Mesh:
    """
    Load an unstructured mesh from a `.su2` file.

    This requires the package `ezmesh` to be installed.

    Args:
        file_or_mesh: Path to `.su2` file or *ezmesh* `Mesh` instance.
        cell_dim: Dimension along which to list the cells. This should be an instance dimension.
        face_format: Sparse storage format for cell connectivity.

    Returns:
        `Mesh`
    """
    if isinstance(file_or_mesh, str):
        from ezmesh import import_from_file
        mesh = import_from_file(file_or_mesh)
    else:
        mesh = file_or_mesh
    if mesh.dim == 2 and mesh.points.shape[-1] == 3:
        points = mesh.points[..., :2]
    else:
        assert mesh.dim == 3, f"Only 2D and 3D meshes are supported but got {mesh.dim} in {file_or_mesh}"
        points = mesh.points
    boundaries = {name.strip(): markers for name, markers in mesh.markers.items()}
    return mesh_from_numpy(points, mesh.elements, boundaries, cell_dim=cell_dim, face_format=face_format)


@broadcast
def load_gmsh(file: str, boundary_names: Sequence[str] = None, periodic: str = None, cell_dim=instance('cells'), face_format: str = 'csc'):
    """
    Load an unstructured mesh from a `.msh` file.

    This requires the package `meshio` to be installed.

    Args:
        file: Path to `.su2` file.
        boundary_names: Boundary identifiers corresponding to the blocks in the file. If not specified, boundaries will be numbered.
        periodic:
        cell_dim: Dimension along which to list the cells. This should be an instance dimension.
        face_format: Sparse storage format for cell connectivity.

    Returns:
        `Mesh`
    """
    import meshio
    from meshio import Mesh
    mesh: Mesh = meshio.read(file)
    dim = max([c.dim for c in mesh.cells])
    if dim == 2 and mesh.points.shape[-1] == 3:
        points = mesh.points[..., :2]
    else:
        assert dim == 3, f"Only 2D and 3D meshes are supported but got {dim} in {file}"
        points = mesh.points
    elements = []
    boundaries = {}
    for cell_block in mesh.cells:
        if cell_block.dim == dim:  # cells
            elements.extend(cell_block.data)
        elif cell_block.dim == dim - 1:
            # derive name from cell_block.tags if present?
            boundary = str(len(boundaries)) if boundary_names is None else boundary_names[len(boundaries)]
            boundaries[boundary] = cell_block.data
        else:
            raise AssertionError(f"Illegal cell block of type {cell_block.type} for {dim}D mesh")
    return mesh_from_numpy(points, elements, boundaries, periodic=periodic, cell_dim=cell_dim, face_format=face_format)


@broadcast
def load_stl(file: str, face_dim=instance('faces')):
    import stl
    model = stl.mesh.Mesh.from_file(file)
    points = np.reshape(model.points, (-1, 3))
    vertices, indices = np.unique(points, axis=0, return_inverse=True)
    indices = np.reshape(indices, (-1, 3))
    mesh = mesh_from_numpy(vertices, indices, element_rank=2, cell_dim=face_dim)
    return mesh


def mesh_from_numpy(points: Sequence[Sequence],
                    polygons: Sequence[Sequence],
                    boundaries: str | Dict[str, List[Sequence]] | None = None,
                    element_rank: int = None,
                    periodic: str = None,
                    cell_dim: Shape = instance('cells'),
                    face_format: str = 'csc') -> Mesh:
    """
    Construct an unstructured mesh from vertices.

    Args:
        points: 2D numpy array of shape (num_points, point_coord).
            The last dimension must have length 2 for 2D meshes and 3 for 3D meshes.
        polygons: List of elements. Each polygon is defined as a sequence of point indices mapping into `points'.
            E.g. `[(0, 1, 2)]` denotes a single triangle connecting points 0, 1, and 2.
        boundaries: An unstructured mesh can have multiple boundaries, each defined by a name `str` and a list of faces, defined by their vertices.
            The `boundaries` `dict` maps boundary names to a list of edges (point pairs) in 2D and faces (3 or more points) in 3D (not yet supported).
        cell_dim: Dimension along which to list the cells. This should be an instance dimension.
        face_format: Storage format for cell connectivity, must be one of `csc`, `coo`, `csr`, `dense`.

    Returns:
        `Mesh`
    """
    cell_dim = cell_dim.with_size(len(polygons))
    points = np.asarray(points)
    xyz = tuple('xyz'[:points.shape[-1]])
    vertices = wrap(points, instance('vertices'), channel(vector=xyz))
    try:  # if all elements have the same vertex count, we stack them
        elements_np = np.stack(polygons).astype(np.int32)
        elements = wrap(elements_np, cell_dim, spatial('vertex_index'))
    except ValueError:
        indices = np.concatenate(polygons)
        vertex_count = np.asarray([len(e) for e in polygons])
        ptr = np.pad(np.cumsum(vertex_count), (1, 0))
        mat = csr_matrix((np.ones(indices.shape, dtype=bool), indices, ptr), shape=(len(polygons), len(points)))
        elements = wrap(mat, cell_dim, instance(vertices).as_dual())
    return mesh(vertices, elements, boundaries, element_rank, periodic, face_format=face_format)


@broadcast(dims=batch)
def mesh(vertices: Geometry | Tensor,
         elements: Tensor,
         boundaries: str | Dict[str, List[Sequence]] | None = None,
         element_rank: int = None,
         periodic: str = None,
         face_format: str = 'csc',
         max_cell_walk: int = None):
    """
    Create a mesh from vertex positions and vertex lists.

    Args:
        vertices: `Tensor` with one instance and one channel dimension `vector`.
        elements: Lists of vertex indices as 2D tensor.
            The elements must be listed along an instance dimension, and the vertex indices belonging to the same polygon must be listed along a spatial dimension.
        boundaries: Pass a `str` to assign one name to all boundary faces.
            For multiple boundaries, pass a `dict` mapping group names `str` to lists of faces, defined by their vertices.
            The last entry can be `None` to group all boundary faces not explicitly listed before.
            The `boundaries` `dict` maps boundary names to a list of edges (point pairs) in 2D and faces (3 or more points) in 3D (not yet supported).
        face_format: Storage format for cell connectivity, must be one of `csc`, `coo`, `csr`, `dense`.

    Returns:
        `Mesh`
    """
    assert 'vector' in channel(vertices), f"vertices must have a channel dimension called 'vector' but got {shape(vertices)}"
    assert instance(vertices), f"vertices must have an instance dimension listing all vertices of the mesh but got {shape(vertices)}"
    if not isinstance(vertices, Geometry):
        vertices = Point(vertices)
    if spatial(elements):  # all elements have same number of vertices
        indices: Tensor = rename_dims(elements, spatial, instance(vertices).as_dual())
        values = expand(True, non_batch(indices))
        elements = CompactSparseTensor(indices, values, instance(vertices).as_dual(), True)
    assert instance(vertices).as_dual() in elements.shape, f"elements must have the instance dim of vertices {instance(vertices)} but got {shape(elements)}"
    if element_rank is None:
        if vertices.vector.size == 2:
            element_rank = 2
        elif vertices.vector.size == 3:
            min_vertices = sum_(elements, instance(vertices).as_dual()).min
            element_rank = 2 if min_vertices <= 4 else 3  # assume tri or quad mesh
        else:
            raise ValueError(vertices.vector.size)
    # --- build faces ---
    periodic_dims = []
    if periodic is not None:
        periodic_dims = [s.strip() for s in periodic.split(',') if s.strip()]
        assert all(p in vertices.vector.item_names for p in periodic_dims), f"Periodic boundaries must be named after axes, e.g. {vertices.vector.item_names} but got {periodic}"
        for base in periodic_dims:
            assert base+'+' in boundaries and base+'-' in boundaries, f"Missing boundaries for periodicity '{base}'. Make sure '{base}+' and '{base}-' are keys in boundaries dict, got {tuple(boundaries)}"
    return Mesh(vertices, elements, element_rank, boundaries, periodic_dims, face_format, max_cell_walk)


def build_faces_2d(vertices: Tensor,  # (vertices:i, vector)
                   elements: Tensor,  # (elements:i, ~vertices)
                   boundaries: Dict[str, Sequence],  # vertex pairs
                   periodic: Sequence[str],  # periodic dim names
                   vertex_mean: Tensor,
                   face_format: str):
    """
    Given a list of vertices, elements and boundary edges, computes the element connectivity matrix  and corresponding edge properties.

    Args:
        vertices: `Tensor` representing list (instance) of vectors (channel)
        elements: Sparse matrix listing all elements (instance). Each entry represents a vertex (dual) belonging to an element.
        boundaries: Named sequences of edges (vertex pairs).
        periodic: Which dims are periodic.
        vertex_mean: Mean vertex position for each element.
        face_format: Sparse matrix format to use for the element-element matrices.
    """
    cell_dim = instance(elements).name
    nb_dim = instance(elements).as_dual().name
    boundaries = {k: wrap(v, 'line:i,vert:i=(start,end)') for k, v in boundaries.items()}
    # --- Periodic: map duplicate vertices to the same index ---
    vertex_id = np.arange(instance(vertices).size)
    for dim in periodic:
        lo_idx, up_idx = boundaries[dim+'-'], boundaries[dim+'+']
        for lo_i, up_i in zip(set(flatten(lo_idx)), set(flatten(up_idx))):
            vertex_id[up_i] = lo_i  # map periodic vertices to one index
    el_coo = to_format(elements, 'coo').numpy().astype(np.int32)
    el_coo.col = vertex_id[el_coo.col]
    # --- Add virtual boundary elements for non-periodic boundaries ---
    boundary_slices = {}
    end = instance(elements).size
    bnd_coo_idx, bnd_coo_vert = [el_coo.row], [el_coo.col]
    for bnd_key, bnd_vertices in boundaries.items():
        if bnd_key[:-1] in periodic:
            continue
        bnd_vert = bnd_vertices.numpy(['line,vert'])
        bnd_idx = np.arange(bnd_vertices.line.size).repeat(2) + end
        bnd_coo_idx.append(bnd_idx)
        bnd_coo_vert.append(bnd_vert)
        boundary_slices[bnd_key] = {nb_dim: slice(end, end+bnd_vertices.line.size)}
        end += bnd_vertices.line.size
    bnd_coo_idx = np.concatenate(bnd_coo_idx)
    bnd_coo_vert = vertex_id[np.concatenate(bnd_coo_vert)]
    bnd_el_coo = coo_matrix((np.ones((bnd_coo_idx.size,), dtype=bool), (bnd_coo_idx, bnd_coo_vert)), shape=(end, instance(vertices).size))
    # --- Compute neighbor elements ---
    num_shared_vertices: csr_matrix = el_coo @ bnd_el_coo.T
    neighbor_filter, = np.where(num_shared_vertices.data == 2)
    src_cell, nb_cell = num_shared_vertices.nonzero()
    src_cell = src_cell[neighbor_filter]
    nb_cell = nb_cell[neighbor_filter]
    connected_elements_coo = coo_matrix((np.ones(src_cell.size, dtype=bool), (src_cell, nb_cell)), shape=num_shared_vertices.shape)
    element_connectivity = wrap(connected_elements_coo, instance(elements).without_sizes() & dual)
    element_connectivity = to_format(element_connectivity, face_format)
    # --- Find vertices for each face pair using 4 alternating patterns: [0101...], [1010...], ["]+[010...], [101...]+["] ---
    bnd_el_coo_v_idx = coo_matrix((bnd_coo_vert+1, (bnd_coo_idx, bnd_coo_vert)), shape=(end, instance(vertices).size))
    ptr = np.cumsum(np.asarray(el_coo.sum(1)))
    first_ptr = np.pad(ptr, (1, 0))[:-1]
    last_ptr = ptr - 1
    alt1 = np.arange(el_coo.data.size) % 2
    alt2 = (1 - alt1)
    alt2[first_ptr] = alt1[first_ptr]
    alt3 = (1 - alt1)
    alt3[last_ptr] = alt1[last_ptr]
    v_indices = []
    for alt in [alt1, (1-alt1), alt2, alt3]:
        el_coo.data = alt + 1e-10
        alt_v_idx = (el_coo @ bnd_el_coo_v_idx.T)
        v_indices.append(alt_v_idx.data[neighbor_filter].astype(np.int32))
    v_indices = np.sort(np.stack(v_indices, -1), axis=1) - 1
    # Cases: 0,i1,i2  |  i1,i1,i2  |  i1,i2,i2  |  i1,i2,i1+i2   (0 is invalid, doubles are invalid)
    # For [1-3]: If self > left and left != 0 and it is the first -> this is the second element.
    first_index = np.argmax((v_indices[:, 1:] > v_indices[:, :-1]) & (v_indices[:, :-1] >= 0), 1)
    v_indices = v_indices[np.arange(v_indices.shape[0]), np.stack([first_index, first_index+1])]
    v_indices = wrap(v_indices, 'vert:i=(start,end),edge:i')
    v_pos = vertices[v_indices]
    if periodic:  # map v_pos: closest to cell_center
        cell_center = vertex_mean[wrap(src_cell, 'edge:i')]
        bounds = bounding_box(vertices)
        delta = PERIODIC.shortest_distance(cell_center - bounds.lower, v_pos - bounds.lower, bounds.size)
        is_periodic = dim_mask(vertices.vector.item_names, periodic)
        v_pos = where(is_periodic, cell_center + delta, v_pos)
    # --- Compute face information ---
    edge_dir = v_pos.vert['end'] - v_pos.vert['start']
    edge_center = .5 * (v_pos.vert['end'] + v_pos.vert['start'])
    edge_len = vec_length(edge_dir)
    normal = vec_normalize(stack([-edge_dir[1], edge_dir[0]], channel(edge_dir)))
    # --- Wrap in sparse matrices ---
    indices = wrap(np.stack([src_cell, nb_cell]), channel(index=(cell_dim, nb_dim)), 'edge:i')
    edge_len = sparse_tensor(indices, edge_len, element_connectivity.shape, format='coo' if face_format == 'dense' else face_format, indices_constant=True)
    normal = tensor_like(edge_len, normal, value_order='original')
    edge_center = tensor_like(edge_len, edge_center, value_order='original')
    vertex_connectivity = None
    return edge_center, normal, edge_len, boundary_slices, vertex_connectivity


def build_mesh(bounds: Box = None,
               resolution=EMPTY_SHAPE,
               obstacles: Union[Geometry, Dict[str, Geometry]] = None,
               method='quad',
               cell_dim: Shape = instance('cells'),
               face_format: str = 'csc',
               max_squish: Optional[float] = .5,
               **resolution_: Union[int, Tensor, tuple, list, Any]) -> Mesh:
    """
    Build a mesh for a given domain, respecting obstacles.

    Args:
        bounds: Bounds for uniform cells.
        resolution: Base resolution
        obstacles: Single `Geometry` or `dict` mapping boundary name to corresponding `Geometry`.
        method: Meshing algorithm. Only `quad` is currently supported.
        cell_dim: Dimension along which to list the cells. This should be an instance dimension.
        face_format: Sparse storage format for cell connectivity.
        max_squish: Smallest allowed cell size compared to the smallest regular cell.
        **resolution_: For uniform grid, pass resolution as `int` and specify `bounds`.
            Or pass a sequence of floats for each dimension, specifying the vertex positions along each axis.
            This allows for variable cell stretching.

    Returns:
        `Mesh`
    """
    if obstacles is None:
        obstacles = {}
    elif isinstance(obstacles, Geometry):
        obstacles = {'obstacle': obstacles}
    assert isinstance(obstacles, dict), f"obstacles needs to be a Geometry or dict"
    if method == 'quad':
        if bounds is None:  # **resolution_ specifies points
            assert not resolution, f"When specifying vertex positions, bounds and resolution will be inferred and must not be specified."
            resolution = spatial(**{dim: non_batch(x).volume for dim, x in resolution_.items()}) - 1
            vert_pos = meshgrid(**resolution_)
            bounds = Box(**{dim: (x[0], x[-1]) for dim, x in resolution_.items()})
            # centroid_x = {dim: .5 * (wrap(x[:-1]) + wrap(x[1:])) for dim, x in resolution_.items()}
            # centroids = meshgrid(**centroid_x)
        else:  # uniform grid from bounds, resolution
            resolution = resolution & spatial(**resolution_)
            vert_pos = meshgrid(resolution + 1) / resolution * bounds.size + bounds.lower
            # centroids = UniformGrid(resolution, bounds).center
        dx = bounds.size / resolution
        regular_size = min(dx, channel)
        vert_pos, polygons, boundaries = build_quadrilaterals(vert_pos, resolution, obstacles, bounds, regular_size * max_squish)
        if max_squish is not None:
            lin_vert_pos = pack_dims(vert_pos, spatial, instance('polygon'))
            corner_pos = lin_vert_pos[polygons]
            min_pos = min(corner_pos, '~polygon')
            max_pos = max(corner_pos, '~polygon')
            cell_sizes = min(max_pos - min_pos, 'vector')
            too_small = cell_sizes < regular_size * max_squish
            # --- remove too small cells ---
            removed = polygons[too_small]
            removed_centers = mean(lin_vert_pos[removed], '~polygon')
            kept_vert = removed[{'~polygon': 0}]
            vert_pos = scatter(lin_vert_pos, kept_vert, removed_centers)
            vertex_map = range(non_channel(lin_vert_pos))
            vertex_map = scatter(vertex_map, rename_dims(removed, '~polygon', instance('poly_list')), expand(kept_vert, instance(poly_list=4)))
            polygons = polygons[~too_small]
            polygons = vertex_map[polygons]
            boundaries = {boundary: vertex_map[edge_list] for boundary, edge_list in boundaries.items()}
            boundaries = {boundary: edge_list[edge_list[{'~vert': 'start'}] != edge_list[{'~vert': 'end'}]] for boundary, edge_list in boundaries.items()}
            # ToDo remove edges which now point to the same vertex
        def build_single_mesh(vert_pos, polygons, boundaries):
            points_np = reshaped_numpy(vert_pos, [..., channel])
            polygon_list = reshaped_numpy(polygons, [..., dual])
            boundaries = {b: edges.numpy('edges,~vert') for b, edges in boundaries.items()}
            return mesh_from_numpy(points_np, polygon_list, boundaries, cell_dim=cell_dim, face_format=face_format)
        return map(build_single_mesh, vert_pos, polygons, boundaries, dims=batch)


def build_quadrilaterals(vert_pos, resolution: Shape, obstacles: Dict[str, Geometry], bounds: Box, min_size) -> Tuple[Tensor, Tensor, dict]:
    vert_id = range_tensor(resolution + 1)
    # --- obstacles: mask and boundaries ---
    boundaries = {}
    full_mask = expand(False, resolution)
    for boundary, obstacle in obstacles.items():
        assert isinstance(obstacle, Geometry), f"all obstacles must be Geometry objects but got {type(obstacle)}"
        active_mask_vert = obstacle.approximate_signed_distance(vert_pos) > min_size
        obs_mask_cell = convolve(active_mask_vert, expand(1, resolution.with_sizes(2))) == 0  # use all cells with one non-blocked vertex
        assert_close(False, obs_mask_cell & full_mask, msg="Obstacles must not overlap. For overlapping obstacles, use union() to assign a single boundary.")
        lo, up = shift(obs_mask_cell, (0, 1), padding=None)
        face_mask = lo != up
        for dim, dim_mask in dict(**face_mask.shift).items():
            face_verts = vert_id[{dim: slice(1, -1)}]
            start_vert = face_verts[{d: slice(None, -1) for d in resolution.names if d != dim}]
            end_vert = face_verts[{d: slice(1, None) for d in resolution.names if d != dim}]
            mask_indices = nonzero(face_mask.shift[dim], list_dim=instance('edges'))
            edges = stack([start_vert[mask_indices], end_vert[mask_indices]], dual(vert='start,end'))
            boundaries.setdefault(boundary, []).append(edges)
            # edge_list = [(s, e) for s, e, m in zip(start_vert, end_vert, dim_mask) if m]
            # boundaries.setdefault(boundary, []).extend(edge_list)
        full_mask |= obs_mask_cell
    boundaries = {boundary: concat(edge_tensors, 'edges') for boundary, edge_tensors in boundaries.items()}
    # --- outer boundaries ---
    def all_faces(ids: Tensor, edge_mask: Tensor, dim):
        assert ids.rank == 1
        mask_indices = nonzero(~edge_mask, list_dim=instance('edges'))
        start_vert = ids[:-1]
        end_vert = ids[1:]
        return stack([start_vert[mask_indices], end_vert[mask_indices]], dual(vert='start,end'))
        # return [(i, j) for i, j, m in zip(ids[:-1], ids[1:], edge_mask) if not m]
    for dim in resolution.names:
        boundaries[dim+'-'] = all_faces(vert_id[{dim: 0}], full_mask[{dim: 0}], dim)
        boundaries[dim+'+'] = all_faces(vert_id[{dim: -1}], full_mask[{dim: -1}], dim)
    # --- cells ---
    cell_indices = nonzero(~full_mask)
    if resolution.rank == 2:
        d1, d2 = resolution.names
        c1 = vert_id[{d1: slice(0, -1), d2: slice(0, -1)}]
        c2 = vert_id[{d1: slice(0, -1), d2: slice(1, None)}]
        c3 = vert_id[{d1: slice(1, None), d2: slice(1, None)}]
        c4 = vert_id[{d1: slice(1, None), d2: slice(0, -1)}]
        polygons = stack([c1, c2, c3, c4], dual('polygon'))
        polygons = polygons[cell_indices]
    else:
        raise NotImplementedError(resolution.rank)
    # --- push vertices out of obstacles ---
    ext_mask = pad(~full_mask, {d: (0, 1) for d in resolution.names}, False)
    has_cell = convolve(ext_mask, expand(1, resolution.with_sizes(2)), extrapolation.ZERO)  # vertices without a cell could be removed to improve memory/cache efficiency
    for obstacle in obstacles.values():
        shifted_verts = obstacle.push(vert_pos)
        vert_pos = where(has_cell, shifted_verts, vert_pos)
    vert_pos = bounds.push(vert_pos, outward=False)
    return vert_pos, polygons, boundaries


def tri_points(mesh: Mesh):
    corners = mesh.vertices.center[mesh.elements._indices]
    assert dual(corners).size == 3, f"signed distance currently only supports triangles"
    return unstack(corners, dual)



def face_curvature(mesh: Mesh):
    v_normals = mesh.elements * si2d(mesh.vertex_normals)
    # v_offsets = mesh.elements * si2d(mesh.vertices.center) - mesh.center

    corners = mesh.vertices.center[mesh.elements._indices]
    assert dual(corners).size == 3, f"signed distance currently only supports triangles"
    A, B, C = unstack(corners.vector.as_dual(), dual(corners))
    e1, e2, e3 = B-A, C-B, A-C
    n1, n2, n3 = unstack(v_normals._values, dual)
    dn1, dn2, dn3 = n2-n1, n3-n2, n1-n3
    curvature_tensor = .5 / mesh.volume * (e1 * dn1 + e2 * dn2 + e3 * dn3)
    scalar_curvature = sum_([curvature_tensor[{'vector': d, '~vector': d}] for d in mesh.vector.item_names], '0')
    return curvature_tensor, scalar_curvature
    # vec_curvature = max(v_normals, dual) - min(v_normals, dual)  # positive / negative


def save_tri_mesh(file: str, mesh: Mesh, **extra_data):
    v = reshaped_numpy(mesh.vertices.center, [instance, 'vector'])
    if isinstance(mesh.elements, CompactSparseTensor):
        f = reshaped_numpy(mesh.elements._indices, [instance, dual])
    else:
        raise NotImplementedError
    print(f"Saving triangle mesh with {v.shape[0]} vertices and {f.shape[0]} faces to {file}")
    os.makedirs(os.path.dirname(file), exist_ok=True)
    np.savez(file, vertices=v, faces=f, f_dim=instance(mesh).name, vertex_dim=instance(mesh.vertices).name, vector=mesh.vector.item_names, has_extra_data=bool(extra_data), **extra_data)


@broadcast
def load_tri_mesh(file: str, convert=False, load_extra=()) -> Mesh | Tuple[Mesh, ...]:
    data = np.load(file, allow_pickle=bool(load_extra))
    f_dim = instance(str(data['f_dim']))
    vertex_dim = instance(str(data['vertex_dim']))
    vector = channel(vector=[str(d) for d in data['vector']])
    faces = tensor(data['faces'], f_dim, spatial('vertex_list'), convert=convert)
    vertices = tensor(data['vertices'], vertex_dim, vector, convert=convert)
    m = mesh(vertices, faces)
    if not load_extra:
        return m
    extra = [data[e] for e in load_extra]
    extra = [e.tolist() if e.dtype == object else e for e in extra]
    return m, *extra


@broadcast(dims=batch)
def decimate_tri_mesh(mesh: Mesh, factor=.1, target_max=10_000,):
    if isinstance(mesh, NoGeometry):
        return mesh
    if instance(mesh).volume == 0:
        return mesh
    import pyfqmr
    mesh_simplifier = pyfqmr.Simplify()
    vertices = reshaped_numpy(mesh.vertices.center, [instance, 'vector'])
    faces = reshaped_numpy(mesh.elements._indices, [instance, dual])
    target_count = min(target_max, int(round(instance(mesh).volume * factor)))
    mesh_simplifier.setMesh(vertices, faces)
    mesh_simplifier.simplify_mesh(target_count=target_count, aggressiveness=7, preserve_border=False)
    vertices, faces, normals = mesh_simplifier.getMesh()
    return mesh_from_numpy(vertices, faces, cell_dim=instance(mesh))
