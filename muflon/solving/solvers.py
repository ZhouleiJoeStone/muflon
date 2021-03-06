# -*- coding: utf-8 -*-

# Copyright (C) 2017 Martin Řehoř
#
# This file is part of MUFLON.
#
# MUFLON is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# MUFLON is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with MUFLON. If not, see <http://www.gnu.org/licenses/>.

"""
This module provides tools for creating solvers based on various discretization
schemes.
"""
import six

from collections import OrderedDict

from dolfin import derivative, lhs, rhs, assemble, dx, begin, end, info
from dolfin import NonlinearVariationalProblem, NonlinearVariationalSolver
from dolfin import NewtonSolver, NonlinearProblem, SystemAssembler
from dolfin import as_backend_type, LUSolver
from dolfin import PETScMatrix, PETScVector, PETScFactory

from fenapack import PCDKSP, PCDAssembler

from muflon.common.boilerplate import not_implemented_msg
from muflon.models.forms import Model

# --- Generic interface for creating demanded systems of PDEs -----------------

class SolverFactory(object):
    """
    Factory for creating solvers.
    """
    factories = {}

    @staticmethod
    def _register(solver):
        """
        Register ``Factory`` of a ``solver``.

        :param solver: name of a specific solver
        :type solver: str
        """
        SolverFactory.factories[solver] = eval(solver + ".Factory()")

    @staticmethod
    def create(model, *args, **kwargs):
        """
        Create an instance of a solver based on the ``model`` and initialize it
        with given arguments.

        Currently implemented solvers:

        * :py:class:`Monolithic`
        * :py:class:`FullyDecoupled`

        :param model: model of the CHNSF type
        :type model: :py:class:`Model <muflon.models.forms.Model>`
        :returns: instance of a specific solver
        :rtype: (subclass of) :py:class:`Solver`
        """
        assert isinstance(model, Model)
        name = kwargs.get("name", None)
        if name is None:
            name = model.discretization_scheme().name()
        if not name in SolverFactory.factories:
            SolverFactory._register(name)
        return SolverFactory.factories[name].create(model, *args, **kwargs)

# --- Generic class for creating solvers ---------------------------------------

class Solver(object):
    """
    This class provides a generic interface for solvers designed for solving
    different variants of CHNSF type models.
    """
    class Factory(object):
        def create(self, *args, **kwargs):
            msg = "Cannot create solver from a generic class."
            not_implemented_msg(self, msg)

    def __init__(self, model, forms=None, name=None, fix_p=False):
        """
        :param model: model of the CHNSF type
        :type model: :py:class:`Model <muflon.models.forms.Model>`
        :param forms: dictonary with items ``'nln'`` and ``'lin'``
                      containing :py:class:`ufl.form.Form` objects
        :type forms: dict
        :param name: name of the solver (if ``None`` then it is extracted
                     from ``model``)
        :type name: str
        :param fix_p: set this parameter to True value if you want to fix
                      the pressure so its mean value is equal to zero
        :type fix_p: bool
        """
        # FIXME: Remove unnecessary kwarg 'name'
        # Get bare version of forms if not given
        if forms is None:
            forms = model.create_forms()

        # Store attributes
        self.data = OrderedDict()
        self.data["model"] = model
        self.data["forms"] = forms

        # Initialize flags
        self._flags = OrderedDict()
        self._flags["fix_p"] = fix_p
        self._flags["setup"] = False

    def comm(self):
        """
        Returns MPI communicator.
        """
        return self.data["model"].discretization_scheme().mesh().mpi_comm()

    def setup(self):
        """
        Override this method to do the additional setup of the solver.
        """
        self._flags["setup"] = True

    def solution_ctl(self):
        """
        Provides access to solution functions at current time level.

        :returns: solution functions at current time level
        :rtype: tuple
        """
        return self.data["model"].discretization_scheme().solution_ctl()

    def refresh(self):
        """
        Put solver into its initial state.
        """
        self._flags["setup"] = False

    def _calibrate_pressure(self, sol_fcn, null_fcn):
        """
        Corrects pressure values so that :math:`\\int_{\\Omega} p \\; dx = 0`.

        :param sol_fcn: "a pointer" to solution function, whose vector of DOF
                        is going to be modified here
        :type sol_fcn: :py:class:`dolfin.Function`
        :param null_fcn: a function representing the constant vector that forms
                         the basis of pressure null space (must respect the
                         shape of ``sol_fcn``, so `axpy` operation may be used)
        :type null_fcn: :py:class:`dolfin.Function`
        """
        DS = self.data["model"].discretization_scheme()
        domain_size = DS.compute_domain_size()
        p = DS.primitive_vars_ctl()["p"].dolfin_repr()
        p_corr = assemble(p*dx)/domain_size
        sol_fcn.vector().axpy(-p_corr, null_fcn.vector())

# --- Monolithic nonlinear solver ---------------------------------------------

class Monolithic(Solver):
    """
    This class implements nonlinear solver for monolithic discretization
    scheme.
    """
    class Factory(object):
        def create(self, *args, **kwargs):
            return Monolithic(*args, **kwargs)

    class Problem(NonlinearProblem):
        """
        Class for interfacing with :py:class:`NewtonSolver`.
        """
        def __init__(self, F, bcs, J, null_space=None):
            super(Monolithic.Problem, self).__init__()
            self.assembler = SystemAssembler(J, F, bcs)
            self.null_space = null_space

        def F(self, b, x):
            self.assembler.assemble(b, x)
            if self.null_space:
                # Orthogonalize RHS vector b with respect to the null space
                self.null_space.orthogonalize(b)

        def J(self, A, x):
            self.assembler.assemble(A)
            if self.null_space:
                # Attach null space to PETSc matrix
                as_backend_type(A).set_nullspace(self.null_space)

    def __init__(self, *args, **kwargs):
        """
        Create nonlinear solver for
        :py:class:`Monolithic <muflon.functions.discretization.Monolithic>`
        discretization scheme.

        See :py:class:`Solver <muflon.solving.solvers.Solver>` for the list of
        valid initialization arguments.
        """
        super(Monolithic, self).__init__(*args, **kwargs)

        # Adjust forms
        DS = self.data["model"].discretization_scheme()
        w = DS.solution_ctl()[0]
        F = self.data["forms"]["nln"]
        J = derivative(F, w)

        # Adjust bcs
        bcs = []
        _bcs = self.data["model"].bcs()
        for bc_v in _bcs.get("v", []):
            assert isinstance(bc_v, tuple)
            assert len(bc_v) == len(w.sub(1))
            bcs += [bc for bc in bc_v if bc is not None]
        bcs += [bc for bc in _bcs.get("p", [])]
        # FIXME: Deal with possible bcs for ``phi`` and ``th``

        # Adjust solver
        solver = NewtonSolver()
        solver.parameters['absolute_tolerance'] = 1E-8
        solver.parameters['relative_tolerance'] = 1E-16
        solver.parameters['maximum_iterations'] = 10
        solver.parameters['linear_solver'] = "mumps"
        #solver.parameters['relaxation_parameter'] = 1.0

        # Preparation for tackling singular systems
        null_space = None
        if self._flags["fix_p"]:
            null_space, null_fcn = DS.build_pressure_null_space()
            self.data["null_fcn"] = null_fcn

        # Store solvers and collect other data
        self.data["solver"] = solver
        self.data["problem"] = Monolithic.Problem(F, bcs, J, null_space)
        self.data["sol_fcn"] = w

    def solve(self):
        """
        Perform one solution step (in time).
        """
        self.data["solver"].solve(
            self.data["problem"], self.data["sol_fcn"].vector())

        if self._flags["fix_p"]:
            self._calibrate_pressure(self.data["sol_fcn"], self.data["null_fcn"])

# --- Semi-decoupled nonlinear solver ---------------------------------------------

class SemiDecoupled(Solver):
    """
    This class implements nonlinear solver for semi-decoupled discretization
    scheme.
    """
    class Factory(object):
        def create(self, *args, **kwargs):
            return SemiDecoupled(*args, **kwargs)

    class CHProblem(NonlinearProblem):
        """Class for interfacing with :py:class:`dolfin.NewtonSolver`."""

        def __init__(self, F, bcs, J):
            super(SemiDecoupled.CHProblem, self).__init__()

            # Assembler for Newton system
            self.assembler = SystemAssembler(J, F, bcs)

            # Store forms for later
            self.forms = {
                "F": F,
                "J": J,
            }

        def function_space(self):
            return self.forms["F"].arguments()[0].function_space()

        def form(self, A, P, b, x):
            if A.empty():
                matA = as_backend_type(A).mat()
                assert not matA.isAssembled()
                bs = self.function_space().dofmap().block_size()
                matA.setBlockSize(bs)
                # matP = as_backend_type(P).mat()
                # assert not matP.isAssembled()
                # matP.setBlockSize(bs)

        def F(self, b, x):
            self.assembler.assemble(b, x)

        def J(self, A, x):
            # matA = as_backend_type(A).mat()
            # bs = self.function_space().dofmap().block_size()
            # assert matA.getBlockSize() == bs
            self.assembler.assemble(A)


    def __init__(self, *args, **kwargs):
        """
        Create solver for
        :py:class:`SemiDecoupled <muflon.functions.discretization.SemiDecoupled>`
        discretization scheme.

        See :py:class:`Solver <muflon.solving.solvers.Solver>` for the list of
        valid initialization arguments.
        """
        super(SemiDecoupled, self).__init__(*args, **kwargs)

        # Extract solution functions
        DS = self.data["model"].discretization_scheme()
        w_ch, w_ns = DS.solution_ctl()

        # Adjust bcs
        bcs_ch = []
        bcs_ns = []
        _bcs = self.data["model"].bcs()
        for bc_v in _bcs.get("v", []):
            assert isinstance(bc_v, tuple)
            assert len(bc_v) == len(w_ns.sub(0))
            bcs_ns += [bc for bc in bc_v if bc is not None]
        bcs_ns += [bc for bc in _bcs.get("p", [])]
        # FIXME: Deal with possible bcs for ``phi`` and ``th``

        # Prepare solver for CH part
        F = self.data["forms"]["nln"]
        J = derivative(F, w_ch)

        # Store solvers and collect other data
        self.data["problem_ch"] = self.CHProblem(F, bcs_ch, J)
        self.data["solver"] = OrderedDict()
        self.data["solver"]["CH"] = OrderedDict()
        self.data["solver"]["CH"]["lin"] = LUSolver("mumps")
        # NOTE: Initialization of nonlinear solver is postponed until setup
        self.data["solver"]["NS"] = LUSolver("mumps")
        self.data["sol_ch"] = w_ch
        self.data["sol_ns"] = w_ns
        self.data["bcs_ns"] = bcs_ns

        # Preparation for tackling singular systems
        if self._flags["fix_p"]:
            null_space, null_fcn = DS.build_pressure_null_space()
            self.data["null_space"] = null_space
            self.data["null_fcn"] = null_fcn

        # Store number of iterations
        self.iters = OrderedDict()
        self.iters["CH"] = [0, 0] # (total, last solve)
        self.iters["NS"] = [0, 0] # (total, last solve)

    def setup(self):
        # Set up nonlinear CH solver based on provided linear solver
        factory = PETScFactory.instance()
        solver_ch = NewtonSolver(
            self.comm(), self.data["solver"]["CH"]["lin"], factory)
        solver_ch.parameters['absolute_tolerance'] = 1E-8
        solver_ch.parameters['relative_tolerance'] = 1E-16
        solver_ch.parameters['maximum_iterations'] = 25
        #solver_ch.parameters['relaxation_parameter'] = 1.0
        #solver_ch.parameters['error_on_nonconvergence'] = False
        #solver_ch.parameters['convergence_criterion'] = 'incremental'
        assert solver_ch.parameters["linear_solver"] == "user_defined"
        assert solver_ch.parameters["preconditioner"] == "user_defined"
        assert "nln" not in self.data["solver"]["CH"]
        self.data["solver"]["CH"]["nln"] = solver_ch

        # Set up NS solver
        if hasattr(self.data["solver"]["NS"], "ksp"):
            ksp = getattr(self.data["solver"]["NS"], "ksp")()
            if isinstance(ksp, PCDKSP):
                forms = self.data["forms"]
                bcs_ns = self.data["bcs_ns"]
                bc_pcd = self.data["model"].bcs()["pcd"]
                self.data["pcd_assembler"] = PCDAssembler(
                    forms["lin"]["lhs"],       # A
                    forms["lin"]["rhs"],       # b
                    bcs_ns,                    # bcs
                    forms["pcd"]["a_pc"],      # P
                    gp=forms["pcd"]["gp"],     # B^T
                    ap=forms["pcd"]["ap"],
                    kp=forms["pcd"]["kp"],
                    mp=forms["pcd"]["mp"],
                    mu=forms["pcd"]["mu"],
                    bcs_pcd=bc_pcd)
                self.data["pcd_assembler"].get_pcd_form("mp").constant = False
                self.data["pcd_assembler"].get_pcd_form("mu").constant = False
                #self.data["pcd_assembler"].get_pcd_form("gp").phantom = False
                self._flags["init_pcd_called"] = False
        else:
            info("")
            info("'LUSolver' will be applied to the Navier-Stokes subproblem.")
            info("")

    def solve(self):
        """
        Perform one solution step (in time).
        """
        begin("Cahn-Hilliard step")
        self.iters["CH"][-1] = self.data["solver"]["CH"]["nln"].solve(
            self.data["problem_ch"], self.data["sol_ch"].vector())[0]
        self.iters["CH"][0] += self.iters["CH"][-1]
        end()

        begin("Navier-Stokes step")
        # Update stabilization terms
        if self.data["model"].parameters["semi"]["sdstab"]:
            self.data["model"]._update_sd_stab_parameter()

        pcd_assembler = self.data.get("pcd_assembler", None)
        if pcd_assembler:
            # Symmetric assembly of the linear system
            A, b = PETScMatrix(self.comm()), PETScVector(self.comm())
            pcd_assembler.system_matrix(A)
            pcd_assembler.rhs_vector(b)
        else:
            # Standard assembly of the linear system
            A = assemble(self.data["forms"]["lin"]["lhs"])
            b = assemble(self.data["forms"]["lin"]["rhs"])
            for bc in self.data["bcs_ns"]:
                bc.apply(A, b)

        if self._flags["fix_p"]:
            # Attach null space to PETSc matrix
            as_backend_type(A).set_nullspace(self.data["null_space"])
            # Orthogonalize RHS vector b with respect to the null space
            self.data["null_space"].orthogonalize(b)

        if pcd_assembler:
            # Symmetric assembly of the preconditioner
            P = PETScMatrix(self.comm())
            pcd_assembler.pc_matrix(P)
            # FIXME: Should we attach the null space also to preconditioner?
            #        Probably not as 'set_nullspace' is related to KSP (not PC).
            # if P.empty():
            #     P = A
            # else:
            #     as_backend_type(P).set_nullspace(self.data["null_space"])
            P = A if P.empty() else P
            # Standard assembly of the preconditioner matrix
            # if self.data["forms"]["pcd"]["a_pc"] is not None:
            #     P = assemble(self.data["forms"]["pcd"]["a_pc"])
            #     for bc in self.data["bcs_ns"]:
            #         bc.apply(P)
            # else:
            #     P = A
            self.data["solver"]["NS"].set_operators(A, P)
            if not self._flags["init_pcd_called"]: # only one call is allowed
                self.data["solver"]["NS"].init_pcd(pcd_assembler)
                self._flags["init_pcd_called"] = True
        else:
            # FIXME: Here we assume that LU solver is used
            assert self.data["solver"]["NS"].parameter_type() == 'lu_solver'
            self.data["solver"]["NS"].set_operator(A)
            # NOTE: Preconditioner matrix can't be set for LUSolver.

        self.iters["NS"][-1] = \
          self.data["solver"]["NS"].solve(self.data["sol_ns"].vector(), b)
        info("Navier-Stokes solver finished in {} iterations".format(self.iters["NS"][-1]))
        self.iters["NS"][0] += self.iters["NS"][-1]

        if self._flags["fix_p"]:
            self._calibrate_pressure(
                self.data["sol_ns"], self.data["null_fcn"])
        end()

# --- FullyDecoupled linear solver --------------------------------------------

class FullyDecoupled(Solver):
    """
    This class implements linear solver for fully decoupled discretization
    scheme.
    """
    class Factory(object):
        def create(self, *args, **kwargs):
            return FullyDecoupled(*args, **kwargs)

    def __init__(self, *args, **kwargs):
        """
        Create linear solver for :py:class:`FullyDecoupled \
        <muflon.functions.discretization.FullyDecoupled>`
        discretization scheme.

        See :py:class:`Solver <muflon.solving.solvers.Solver>` for the list of
        valid initialization arguments.
        """
        super(FullyDecoupled, self).__init__(*args, **kwargs)

        # Create solvers
        solver = OrderedDict()
        solver["phi"] = LUSolver("mumps")
        solver["chi"] = LUSolver("mumps")
        solver["v"]   = LUSolver("mumps")
        solver["p"]   = LUSolver("mumps")
        self.data["solver"] = solver

        # Initialize flags
        self._flags["setup"] = False

    def setup(self):
        """
        Pre-assemble time independent matrices, group right hand sides and
        solution functions.
        """
        DS = self.data["model"].discretization_scheme()
        w = DS.solution_ctl()
        eqn = self.data["forms"]["lin"]
        pv = DS.primitive_vars_ctl()
        n = len(pv["phi"]) # n = N - 1
        gdim = len(pv["v"])

        _A   = OrderedDict(phi=[], chi=[], v=[])
        _rhs = OrderedDict(phi=[], chi=[], v=[])
        _sol = OrderedDict(phi=[], chi=[], v=[])
        _bcs = OrderedDict(sorted(six.iteritems(self.data["model"].bcs())))
        for i in range(n):
            _A["phi"].append(assemble(eqn["lhs"][i]))
            _A["chi"].append(assemble(eqn["lhs"][n+i]))
            _rhs["phi"].append(eqn["rhs"][i])
            _rhs["chi"].append(eqn["rhs"][n+i])
            _sol["phi"].append(w[i])
            _sol["chi"].append(w[n+i])
        for i in range(gdim): # TODO: We know that A_v2 = A_v1
            _A["v"].append(assemble(eqn["lhs"][2*n+i]))
            _rhs["v"].append(eqn["rhs"][2*n+i])
            _sol["v"].append(w[2*n+i])
            for bc in _bcs.get("v", []):
                if bc[i] is not None:
                    bc[i].apply(_A["v"][-1])
        _A["p"] = assemble(eqn["lhs"][2*n+gdim])
        for bc in _bcs.get("p", []):
            bc.apply(_A["p"])
        _rhs["p"] = eqn["rhs"][2*n+gdim]
        _sol["p"] = w[2*n+gdim]
        # FIXME: Deal with possible bcs for ``phi`` and ``th``

        # Preparation for tackling singular systems
        if self._flags["fix_p"]:
            null_space, null_fcn = DS.build_pressure_null_space()
            self.data["null_space"] = null_space
            self.data["null_fcn"] = null_fcn
            # Attach null space to PETSc matrix
            as_backend_type(_A["p"]).set_nullspace(null_space)

        # Store matrices + grouped right hand sides and solution functions
        self.data["A"]   = _A
        self.data["rhs"] = _rhs
        self.data["sol"] = _sol
        self.data["bcs"] = _bcs

        self._flags["setup"] = True

    def solve(self):
        """
        Perform one solution step (in time).
        """
        solver = self.data["solver"]
        begin("Advance-phase")
        for i, A in enumerate(self.data["A"]["chi"]):
            b = assemble(self.data["rhs"]["chi"][i])
            solver["chi"].solve(A, self.data["sol"]["chi"][i].vector(), b)
        for i, A in enumerate(self.data["A"]["phi"]):
            b = assemble(self.data["rhs"]["phi"][i])
            solver["phi"].solve(A, self.data["sol"]["phi"][i].vector(), b)
        end()

        begin("Pressure step")
        b = assemble(self.data["rhs"]["p"])
        for bc in self.data["bcs"].get("p", []):
            bc.apply(b)
        if self._flags["fix_p"]:
            # Orthogonalize RHS vector b with respect to the null space
            self.data["null_space"].orthogonalize(b)

        solver["p"].solve(
            self.data["A"]["p"], self.data["sol"]["p"].vector(), b)

        if self._flags["fix_p"]:
            self._calibrate_pressure(
                self.data["sol"]["p"], self.data["null_fcn"])
        end()

        begin("Velocity step")
        for i, A in enumerate(self.data["A"]["v"]):
            b = assemble(self.data["rhs"]["v"][i])
            # FIXME: How to apply bcs in a symmetric fashion?
            for bc in self.data["bcs"].get("v", []):
                if bc[i] is not None:
                    bc[i].apply(b)
            solver["v"].solve(A, self.data["sol"]["v"][i].vector(), b)
        end()
