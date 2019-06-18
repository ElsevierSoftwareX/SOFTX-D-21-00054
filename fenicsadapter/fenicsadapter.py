"""This module handles CustomExpression and initalization of the FEniCS
adapter.

:raise ImportError: if PRECICE_ROOT is not defined
"""
import dolfin
from dolfin import UserExpression, SubDomain, Function, Constant
from scipy.interpolate import Rbf
from scipy.interpolate import interp1d
import numpy as np
from .config import Config
from .checkpointing import Checkpoint
from .solverstate import SolverState


import fenicsadapter.waveform_bindings

import logging

logging.basicConfig(level=logging.INFO)


class CustomExpression(UserExpression):
    """Creates functional representation (for FEniCS) of nodal data
    provided by preCICE, using RBF interpolation.
    """
    def set_boundary_data(self, vals, coords_x, coords_y=None, coords_z=None):
        self.update_boundary_data(vals, coords_x, coords_y, coords_z)

    def update_boundary_data(self, vals, coords_x, coords_y=None, coords_z=None):
        self._coords_x = coords_x
        if coords_y is None:
            coords_y = np.zeros(self._coords_x.shape)
        self._coords_y = coords_y
        if coords_z is None:
            coords_z = np.zeros(self._coords_x.shape)
        self._coords_z = coords_z

        self._vals = vals.flatten()
        assert (self._vals.shape == self._coords_x.shape)

    def rbf_interpol(self, x):
        if x.__len__() == 1:
            f = Rbf(self._coords_x, self._vals.flatten())
            return f(x)
        if x.__len__() == 2:
            f = Rbf(self._coords_x, self._coords_y, self._vals.flatten())
            return f(x[0], x[1])
        if x.__len__() == 3:
            f = Rbf(self._coords_x, self._coords_y, self._coords_z, self._vals.flatten())
            return f(x[0], x[1], x[2])

    def lin_interpol(self, x):
        f = interp1d(self._coords_y, self._vals, bounds_error=False, fill_value="extrapolate")
        return f(x[1])

    def eval(self, value, x):
        value[0] = self.lin_interpol(x)


class Adapter:
    """Initializes the Adapter. Initalizer creates object of class Config (from
    config.py module).

    :ivar _config: object of class Config, which stores data from the JSON config file
    """
    def __init__(self, adapter_config_filename, other_adapter_config_filename):

        self._config = Config(adapter_config_filename)

        self._solver_name = self._config.get_solver_name()

        self._interface = fenicsadapter.waveform_bindings.WaveformBindings(self._solver_name, 0, 1)
        self._interface.configure_waveform_relaxation(adapter_config_filename, other_adapter_config_filename)
        self._interface.configure(self._config.get_config_file_name())
        self._dimensions = self._interface.get_dimensions()

        self._coupling_subdomain = None  # initialized later
        self._mesh_fenics = None  # initialized later
        self._coupling_bc_expression = None  # initialized later

        # coupling mesh related quantities
        self._coupling_mesh_vertices = None  # initialized later
        self._mesh_name = self._config.get_coupling_mesh_name()
        self._mesh_id = self._interface.get_mesh_id(self._mesh_name)
        self._vertex_ids = None # initialized later
        self._n_vertices = None # initialized later

        # write data related quantities (write data is written by this solver to preCICE)
        self._write_data_name = self._config.get_write_data_name()
        self._write_data = None

        # read data related quantities (read data is read by this solver from preCICE)
        self._read_data_name = self._config.get_read_data_name()
        self._read_data = None

        # numerics
        self._precice_tau = None

        ## checkpointing
        self._checkpoint = Checkpoint()

    def convert_fenics_to_precice(self, data, mesh, subdomain):
        """Converts FEniCS data of type dolfin.Function into
        Numpy array for all x and y coordinates on the boundary.

        :param data: FEniCS boundary function
        :raise Exception: if type of data cannot be handled
        :return: array of FEniCS function values at each point on the boundary
        """
        if type(data) is dolfin.Function:
            x_all, y_all = self.extract_coupling_boundary_coordinates()
            return np.array([data(x, y) for x, y in zip(x_all, y_all)])
        else:
            raise Exception("Cannot handle data type %s" % type(data))

    def extract_coupling_boundary_vertices(self):
        """Extracts verticies which lay on the boundary. Currently handles 2D
        case properly, 3D is circumvented.

        :raise Exception: if no correct coupling interface is defined
        :return: stack of verticies
        """
        n = 0
        vertices_x = []
        vertices_y = []
        if self._dimensions == 3:
            vertices_z = []

        if not issubclass(type(self._coupling_subdomain), SubDomain):
            raise Exception("no correct coupling interface defined!")

        for v in dolfin.vertices(self._mesh_fenics):
            if self._coupling_subdomain.inside(v.point(), True):
                n += 1
                vertices_x.append(v.x(0))
                vertices_y.append(v.x(1))
                if self._dimensions == 3:
                    # todo this has to be fixed for "proper" 3D coupling. Currently this is a workaround for the coupling of 2D fenics with pseudo 3D openfoam
                    vertices_z.append(0)

        if self._dimensions == 2:
            return np.stack([vertices_x, vertices_y]), n
        elif self._dimensions == 3:
            return np.stack([vertices_x, vertices_y, vertices_z]), n

    def set_coupling_mesh(self, mesh, subdomain):
        """Sets the coupling mesh. Called by initalize() function at the
        beginning of the simulation.
        """
        self._coupling_subdomain = subdomain
        self._mesh_fenics = mesh
        self._coupling_mesh_vertices, self._n_vertices = self.extract_coupling_boundary_vertices()
        self._vertex_ids = np.zeros(self._n_vertices)
        self._interface.set_mesh_vertices(self._mesh_id, self._n_vertices, self._coupling_mesh_vertices.flatten('F'), self._vertex_ids)

    def set_write_field(self, write_function_init):
        """Sets the write field. Called by initalize() function at the
        beginning of the simulation.

        :param write_function_init: function on the write field
        """
        self._write_data = self.convert_fenics_to_precice(write_function_init, self._mesh_fenics, self._coupling_subdomain)

    def set_read_field(self, read_function_init):
        """Sets the read field. Called by initalize() function at the
        beginning of the simulation.

        :param read_function_init: function on the read field
        """
        self._read_data = self.convert_fenics_to_precice(read_function_init, self._mesh_fenics, self._coupling_subdomain)

    def create_coupling_boundary_condition(self):
        """Creates the coupling boundary conditions using CustomExpression."""
        x_vert, y_vert = self.extract_coupling_boundary_coordinates()

        try:  # works with dolfin 1.6.0
            self._coupling_bc_expression = CustomExpression()
        except (TypeError, KeyError):  # works with dolfin 2017.2.0
            self._coupling_bc_expression = CustomExpression(degree=0)

        self._coupling_bc_expression.set_boundary_data(self._read_data, x_vert, y_vert)

    def create_coupling_dirichlet_boundary_condition(self, function_space):
        """Creates the coupling Dirichlet boundary conditions using
        create_coupling_boundary_condition() method.

        :return: dolfin.DirichletBC()
        """
        self.create_coupling_boundary_condition()
        return dolfin.DirichletBC(function_space, self._coupling_bc_expression, self._coupling_subdomain)

    def create_coupling_neumann_boundary_condition(self, test_functions):
        """Creates the coupling Neumann boundary conditions using
        create_coupling_boundary_condition() method.

        :return: expression in form of integral: g*v*ds. (see e.g. p. 83ff
         Langtangen, Hans Petter, and Anders Logg. "Solving PDEs in Python The
         FEniCS Tutorial Volume I." (2016).)
        """
        self.create_coupling_boundary_condition()
        return self._coupling_bc_expression * test_functions * dolfin.ds  # this term has to be added to weak form to add a Neumann BC (see e.g. p. 83ff Langtangen, Hans Petter, and Anders Logg. "Solving PDEs in Python The FEniCS Tutorial Volume I." (2016).)

    def _restore_solver_state_from_checkpoint(self, state):
        """Resets the solver's state to the checkpoint's state.
        :param state: current state of the FEniCS solver
        """
        logging.debug("Restore solver state")
        state.update(self._checkpoint.get_state())
        self._interface.fulfilled_action(fenicsadapter.waveform_bindings.action_read_iteration_checkpoint())

    def _advance_solver_state(self, state, u_np1, dt):
        """Advances the solver's state by one timestep.
        :param state: old state
        :param u_np1: new value
        :param dt: timestep size
        :return:
        """
        logging.debug("Advance solver state")
        logging.debug("old state: t={time}".format(time=state.t))
        state.update(SolverState(u_np1, state.t + dt, self._checkpoint.get_state().n + 1))
        logging.debug("new state: t={time}".format(time=state.t))

    def _save_solver_state_to_checkpoint(self, state):
        """Writes given solver state to checkpoint.
        :param state: state being saved as checkpoint
        """
        logging.debug("Save solver state")
        self._checkpoint.write(state)
        self._interface.fulfilled_action(fenicsadapter.waveform_bindings.action_write_iteration_checkpoint())

    def advance(self, write_function, u_np1, u_n, t, dt, n):
        """Calls preCICE advance function using precice and manages checkpointing.
        The solution u_n is updated by this function via call-by-reference. The corresponding values for t and n are returned.

        This means:
        * either, the olf value of the checkpoint is assigned to u_n to repeat the iteration,
        * or u_n+1 is assigned to u_n and the checkpoint is updated correspondingly.

        :param write_function: a FEniCS function being sent to the other participant as boundary condition at the coupling interface
        :param u_np1: new value of FEniCS solution u_n+1 at time t_n+1 = t+dt
        :param u_n: old value of FEniCS solution u_n at time t_n = t; updated via call-by-reference
        :param t: current time t_n for timestep n
        :param dt: timestep size dt = t_n+1 - t_n
        :param n: current timestep
        :return: return starting time t and timestep n for next FEniCS solver iteration. u_n is updated by advance correspondingly.
        """

        state = SolverState(u_n, t, n)

        # sample write data at interface
        x_vert, y_vert = self.extract_coupling_boundary_coordinates()
        self._write_data = self.convert_fenics_to_precice(write_function, self._mesh_fenics, self._coupling_subdomain)
        if True:  # todo: add self._interface.is_write_data_required(dt). We should add this check. However, it is currently not properly implemented for waveform relaxation
            self._interface.write_block_scalar_data(self._write_data_name, self._mesh_id, self._n_vertices, self._vertex_ids, self._write_data, t + dt)
        max_dt = self._interface.advance(dt)

        precice_step_complete = False
        solver_state_has_been_restored = False
        
        # checkpointing
        if self._interface.is_action_required(fenicsadapter.waveform_bindings.action_read_iteration_checkpoint()):
            self._restore_solver_state_from_checkpoint(state)
            solver_state_has_been_restored = True
        else:
            self._advance_solver_state(state, u_np1, dt)

        if self._interface.is_action_required(fenicsadapter.waveform_bindings.action_write_iteration_checkpoint()):
            assert (not solver_state_has_been_restored)  # avoids invalid control flow
            self._save_solver_state_to_checkpoint(state)
            precice_step_complete = True

        _, t, n = state.get_state()

        if True:  # todo: add self._interface.is_read_data_available().  We should add this check. However, it is currently not properly implemented for waveform relaxation
            self._interface.read_block_scalar_data(self._read_data_name, self._mesh_id, self._n_vertices, self._vertex_ids, self._read_data, t + dt)  # if precice_step_complete, we have to already use the new t for reading. Otherwise, we get a lag. Therefore, this command has to be called AFTER the state has been updated/recovered.
        print(self._read_data)

        self._coupling_bc_expression.update_boundary_data(self._read_data, x_vert, y_vert)

        return t, n, precice_step_complete, max_dt

    def initialize(self, coupling_subdomain, mesh, read_field, write_field, u_n, t=0, n=0):
        """Initializes remaining attributes. Called once, from the solver.
        :param read_field: function applied on the read field
        :param write_field: function applied on the write field
        """
        self.set_coupling_mesh(mesh, coupling_subdomain)
        self.set_read_field(read_field)
        self.set_write_field(write_field)
        self._precice_tau = self._interface.initialize()

        dt = Constant(0)
        self.fenics_dt = self._precice_tau / self._config.get_n_substeps()
        dt.assign(np.min([self.fenics_dt, self._precice_tau]))

        self._interface.initialize_waveforms(self._mesh_id, self._n_vertices, self._vertex_ids, self._write_data_name,
                                             self._read_data_name)

        if self._interface.is_action_required(fenicsadapter.waveform_bindings.action_write_initial_data()):
            self._interface.write_block_scalar_data(self._write_data_name, self._mesh_id, self._n_vertices, self._vertex_ids, self._write_data, t)
            self._interface.fulfilled_action(fenicsadapter.waveform_bindings.action_write_initial_data())

        self._interface.initialize_data(read_zero=self._read_data, write_zero=self._write_data)

        if self._interface.is_read_data_available():
            self._interface.read_block_scalar_data(self._read_data_name, self._mesh_id, self._n_vertices, self._vertex_ids, self._read_data, t + dt(0))
            print(self._read_data)

        if self._interface.is_action_required(fenicsadapter.waveform_bindings.action_write_iteration_checkpoint()):
            initial_state = SolverState(u_n, t, n)
            self._save_solver_state_to_checkpoint(initial_state)

        return dt

    def is_coupling_ongoing(self):
        """Determines whether simulation should continue. Called from the
        simulation loop in the solver.

        :return: True if the coupling is ongoing, False otherwise
        """
        return self._interface.is_coupling_ongoing()

    def extract_coupling_boundary_coordinates(self):
        """Extracts the coordinates of vertices that lay on the boundary. 3D
        case currently handled as 2D.

        :return: x and y cooridinates.
        """
        vertices, _ = self.extract_coupling_boundary_vertices()
        vertices_x = vertices[0, :]
        vertices_y = vertices[1, :]
        if self._dimensions == 3:
            vertices_z = vertices[2, :]

        if self._dimensions == 2:
            return vertices_x, vertices_y
        elif self._dimensions == 3:
            # todo this has to be fixed for "proper" 3D coupling. Currently this is a workaround for the coupling of 2D fenics with pseudo 3D openfoam
            return vertices_x, vertices_y

    def finalize(self):
        """Finalizes the coupling interface."""
        self._interface.finalize()
