import numpy as np
import sympy
from skimage.measure import marching_cubes
# units
from scipy.constants import hbar, eV, angstrom
# type hinting
from collections.abc import Collection
# default dictionary for point hashing
from collections import defaultdict
# conversion from energy gradient units to m/s for velocity
velocity_units = 1e-3 * eV * angstrom / hbar


class BandStructure:
    """
    Contains bandstructure information for a given material.

    In addition to the dispersion relation and general parameters, this
    class also contains methods for discretizing the Fermi surface and
    calculating electronic properties.

    Parameters
    ----------
    dispersion : str
        The dispersion relation. Expresses the dispersion relation
        in terms of symbols in `wavevector_names` and additional
        parameters in `bandparams`. It must be parsable and
        differentiable by `sympy`. Energy units are milli eV.
    chemical_potential : float
        The chemical potential in milli eV.
    unit_cell : Collection[float]
        The dimensions of the unit cell in angstrom.
    atoms_per_cell : int, optional
        The number of atoms in the specified unit cell. This is not
        necessarily the exact number of atoms; it should be the number
        of conducting units in the cell. So, for example, this is equal
        to 2 for LSCO, which has the cuprate atoms in a BCC cell.
    bandparams : dict, optional
        The parameters of the dispersion relation. Energy units are
        milli eV and distance units are angstrom.
    axis_names : str or Collection[str], optional
        The names of the unit cell axes. Must be parsable by
        `sympy.symbols`.
    wavevector_names : str or Collection[str], optional
        The names of the wavevector components. Must be parsable by
        `sympy.symbols`.
    resolution :  int or Collection[int], optional
        Controls the resolution of the grids used for discretizing the
        Fermi surface. If a collection of integers is provided, each
        element corresponds to the resolution along the respective
        axis. If a single integer is provided, it is used for all axes.
    ncorrect : int, optional
        The number of correction steps for improving the accuracy of
        the discretization of the Fermi surface.

    Attributes
    ----------
    dispersion : str
        The dispersion relation. Updating this will automatically
        update `energy_func` and `velocity_func`.
    chemical_potential : float
        The chemical potential in milli eV.
    unit_cell : Collection[float]
        The dimensions of the unit cell in angstrom.
    atoms_per_cell : int
        The number of atoms (more precisely, the number of conducting
        units) in the specified unit cell.
    bandparams : dict
        The parameters of the dispersion relation. Updating this 
        will automatically update `energy_func` and `velocity_func`.
    energy_func : function
        The energy function for the dispersion relation. Takes
        kx, ky, and kz in angstrome^-1 as arguments and returns
        the energy in milli eV.
    velocity_func : function
        The velocity function for the dispersion relation. Takes
        kx, ky, and kz in angstrome^-1 as arguments and returns
        the velocity vector as a list [vx, vy, vz] in units of m/s.
    kpoints : (N, 3) numpy.ndarray
        The discretized k-points on the Fermi surface. Each row
        corresponds to a k-point in the form [kx, ky, kz].
    kfaces : (F, 3) numpy.ndarray
        The faces of the triangulated surface in k-space. Each row
        corresponds to a face in the form [i, j, k], where i, j,
        and k are the indices of the vertices of the face in the
        `kpoints` array.
    kpoints_periodic : (N, 3) numpy.ndarray of float
        The kpoints on the Fermi surface with the duplicate boundary
        points removed.
    kfaces_periodic : (F, 3) numpy.ndarray of int
        Same as `kfaces`, but points to the unique points in
        kpoints_periodic.
    resolution : int or Collection[int]
        The resolution of the grids used for approximating the Fermi
        surface geometry with the marching cubes algorithm.
    ncorrect : int
        The number of Newton--Raphson steps applied to correct the
        triangulated surface after the marching cubes algorithm.
    axis_names : str or Collection[str]
        The names of the unit cell axes.
    wavevector_names : str or Collection[str]
        The names of the wavevector components.
    """
    def __init__(
            self, dispersion: str, chemical_potential: float,
            unit_cell: Collection[float], atoms_per_cell: int = 1,
            bandparams: dict = {},
            axis_names: Collection[str] | str = ['a', 'b', 'c'],
            wavevector_names: Collection[str] | str = ['kx', 'ky', 'kz'],
            resolution: int = 20, ncorrect=2, **kwargs):
        # avoid triggering the __setattr__ method for the first time
        super().__setattr__('dispersion', dispersion)
        super().__setattr__('bandparams', bandparams)
        self.chemical_potential = chemical_potential
        self.unit_cell = unit_cell
        self.atoms_per_cell = atoms_per_cell
        self.axis_names = axis_names
        self.wavevector_names = wavevector_names
        self.resolution = resolution
        self.correction_steps = ncorrect
        self._parse_dispersion()
        self.kpoints = np.empty((0, 3))

    def __setattr__(self, name, value):
        if name == 'dispersion' or name == 'bandparams':
            super().__setattr__(name, value)
            self._parse_dispersion()
        if name in ['chemical_potential', 'unit_cell', 'resolution']:
            self.kpoints = np.empty((0, 3))
        super().__setattr__(name, value)
    
    def discretize(self):
        """
        Discretize the Fermi surface.

        First, the surface is triangulated using the marching cubes
        algorithm with `resolution` controlling the resolution of the
        grid. Next, to improve the accuracy of the isosurface,
        `ncorrect` steps of the Newton--Raphson root-finding method are
        applied to the output of marching cubes. Finally, after the
        surface construction, periodic boundary conditions are applied
        to "stitch" the open ends of the surface together.
        """
        self._gvec = np.array([np.pi / a for a in self.unit_cell])
        self._voxel_size = 2 * self._gvec / (self.resolution-1)
        # Make resolution even to ensure symmetry in the grid
        resolution = self.resolution + self.resolution % 2

        self.kpoints, self.kfaces, _, _ = marching_cubes(
            self.energy_func(*np.mgrid[
                -self._gvec[0]:self._gvec[0]:1j*resolution,
                -self._gvec[1]:self._gvec[1]:1j*resolution,
                -self._gvec[2]:self._gvec[2]:1j*resolution]),
            level=self.chemical_potential)
        self.kpoints *= 2 * self._gvec[None, :] / (resolution-1)
        self.kpoints -= self._gvec[None, :]

        for _ in range(self.correction_steps):
            self._apply_newton_correction()
        self._stitch_periodic_boundaries()

    def periodic_distance(self, k1: np.ndarray, k2: np.ndarray,
                          broadcast: bool = False) -> np.ndarray:
        """
        Calculate the periodic distance between two k-points.

        Parameters
        ----------
        k1 : np.ndarray
            The first k-point.
        k2 : np.ndarray
            The second k-point.
        broadcast : bool, optional
            If True, assumes that k1 and k2 are 2D arrays containing
            multiple k-points and broadcasts the calculation
            accordingly. If False, assumes that k1 and k2 are 1D
            arrays containing a single k-point.

        Returns
        -------
        float
            k2 - k1 with periodic boundary conditions applied.
        """
        gvec = self._gvec[None, :] if broadcast else self._gvec
        kdiff = k2 - k1
        kdiff += gvec
        kdiff %= 2 * gvec
        kdiff -= gvec
        return kdiff

    def calculate_mass(self):
        """
        Calculate the effective mass of the charge carries.

        Returns
        -------
        float
            The effective mass divided by the rest mass of
            the electron, m_e.
        """
        # Placeholder for actual calculation
        return 0.0
    
    def _parse_dispersion(self):
        """
        Parse the dispersion relation and extract the necessary
        information for further calculations.
        """
        ksymbols = sympy.symbols(self.wavevector_names)
        all_symbols = (ksymbols + sympy.symbols(self.axis_names)
                       + sympy.symbols(list(self.bandparams.keys())))
        self._energy_sympy = sympy.sympify(self.dispersion)
        self._velocities_sympy = [
            sympy.diff(self._energy_sympy, k) * velocity_units
            for k in sympy.symbols(self.wavevector_names)]
        for i, v in enumerate(self._velocities_sympy):
            if v == 0:
                self._velocities_sympy[i] = f"numpy.zeros_like({ksymbols[i]})"
        self._energy_func_full = sympy.lambdify(
            all_symbols, self._energy_sympy)
        self._velocity_funcs_full = [
            sympy.lambdify(all_symbols, vexpr, 'numpy')
            for vexpr in self._velocities_sympy]
        self.energy_func = lambda kx, ky, kz: self._energy_func_full(
            kx, ky, kz, *self.unit_cell, **self.bandparams)
        self.velocity_func = lambda kx, ky, kz: [
            vfunc(kx, ky, kz, *self.unit_cell, **self.bandparams)
            for vfunc in self._velocity_funcs_full]

    def _stitch_periodic_boundaries(self, threshold=1e-5):
        """
        Find duplicate points on the periodic boundaries, then make the
        periodic mesh arrays. Threshold sets the fraction of the
        resolution that we consider the points to be the same if they
        are within that distance.
        """
        voxel_coordinates = np.round((self.kpoints+self._gvec[None,:])
                                     / (threshold*self._voxel_size[None,:]))
        voxel_coordinates %= np.round(
            2*self._gvec / (threshold*self._voxel_size))[None, :]
        point_bins = defaultdict(list)
        duplicate_points = dict()
        for i, coordinates in enumerate(voxel_coordinates):
            point_bins[tuple(coordinates)].append(i)
        for coordinates in point_bins:
            point_bin = point_bins[coordinates]
            num_neighbors = len(point_bin)
            if num_neighbors > 1:
                primary_point = point_bin[0]
                for point in point_bin[1:]:
                    duplicate_points[point] = primary_point
        self._build_periodic_mesh(duplicate_points)
    
    def _build_periodic_mesh(self, duplicate_points):
        """
        Build the periodic kpoints and kfaces arrays by removing
        duplicate points and reindexing.
        """
        unique_mask = np.full(len(self.kpoints), True)
        unique_mask[list(duplicate_points.keys())] = False
        self.kpoints_periodic = self.kpoints[unique_mask]
        reindex_map = np.cumsum(unique_mask) - 1
        self.kfaces_periodic = np.empty_like(self.kfaces)
        for i, face in enumerate(self.kfaces):
            for j, point in enumerate(face):
                if point in duplicate_points:
                    reindex_map[point] = reindex_map[
                        duplicate_points[point]]
                self.kfaces_periodic[i, j] = reindex_map[point]
    
    def _apply_newton_correction(self):
        residuals = self.energy_func(
            self.kpoints[:, 0], self.kpoints[:, 1], self.kpoints[:, 2]
            ) - self.chemical_potential
        gradients = np.column_stack(self.velocity_func(
            self.kpoints[:, 0], self.kpoints[:, 1], self.kpoints[:, 2])
            ) / velocity_units
        gradient_norms = np.linalg.norm(gradients, axis=-1)
        self.kpoints -= (residuals / gradient_norms**2)[:, None] * gradients
