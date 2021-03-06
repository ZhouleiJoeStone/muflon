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
Method of Manufactured Solutions - test case Ia.
"""

from __future__ import print_function

import pytest
import os
import gc
import six
import itertools

from dolfin import *
from matplotlib import pyplot, gridspec

from fenapack import PCDKrylovSolver

from muflon import mpset
from muflon import DiscretizationFactory, SimpleCppIC
from muflon import ModelFactory
from muflon import SolverFactory
from muflon import TimeSteppingFactory, TSHook
from muflon import DoublewellFactory, multiwell
from muflon import capillary_force, total_flux

from muflon.utils.testing import GenericBenchPostprocessor

# FIXME: remove the following workaround
from muflon.common.timer import Timer

parameters["form_compiler"]["representation"] = "uflacs"
parameters["form_compiler"]["optimize"] = True

def create_domain(refinement_level):
    # Prepare mesh
    nx = 2*(2**(refinement_level))
    mesh = RectangleMesh(Point(0., -1.), Point(2., 1.), nx, nx) #, 'crossed'
    del nx

    # Define and mark boundaries
    class Gamma0(SubDomain):
        def inside(self, x, on_boundary):
            return on_boundary
    boundary_markers = MeshFunction("size_t", mesh, mesh.topology().dim()-1)
    boundary_markers.set_all(3)        # interior facets
    Gamma0().mark(boundary_markers, 0) # boundary facets

    return mesh, boundary_markers

def create_discretization(scheme, mesh, k=1):
    # Prepare finite elements
    Pk = FiniteElement("Lagrange", mesh.ufl_cell(), k)
    Pk1 = FiniteElement("Lagrange", mesh.ufl_cell(), k+1)

    return DiscretizationFactory.create(scheme, mesh, Pk, Pk, Pk1, Pk)

def create_manufactured_solution():
    coeffs_NS = dict(A0=2.0, a0=pi, b0=pi, w0=1.0)
    ms = {}
    ms["v1"] = {"expr": "A0*sin(a0*x[0])*cos(b0*x[1])*sin(w0*t)",
                "prms": coeffs_NS}
    ms["v2"] = {"expr": "-(A0*a0/b0)*cos(a0*x[0])*sin(b0*x[1])*sin(w0*t)",
                "prms": coeffs_NS}
    ms["p"] = {"expr": "A0*sin(a0*x[0])*sin(b0*x[1])*cos(w0*t)",
               "prms": coeffs_NS}
    ms["phi1"] = {"expr": "(1.0 + A1*cos(a1*x[0])*cos(b1*x[1])*sin(w1*t))/6.0",
                  "prms": dict(A1=1.0, a1=pi, b1=pi, w1=1.0)}
    ms["phi2"] = {"expr": "(1.0 + A2*cos(a2*x[0])*cos(b2*x[1])*sin(w2*t))/6.0",
                  "prms": dict(A2=1.0, a2=pi, b2=pi, w2=1.2)}
    ms["phi3"] = {"expr": "(1.0 + A3*cos(a3*x[0])*cos(b3*x[1])*sin(w3*t))/6.0",
                  "prms": dict(A3=1.0, a3=pi, b3=pi, w3=0.8)}
    return ms

def create_exact_solution(ms, FE, degrise=3):
    es = {}
    es["phi1"] = Expression(ms["phi1"]["expr"], #element=FE["phi"],
                            degree=FE["phi"].degree()+degrise,
                            cell=FE["phi"].cell(),
                            t=0.0, **ms["phi1"]["prms"])
    es["phi2"] = Expression(ms["phi2"]["expr"], #element=FE["phi"],
                            degree=FE["phi"].degree()+degrise,
                            cell=FE["phi"].cell(),
                            t=0.0, **ms["phi2"]["prms"])
    es["phi3"] = Expression(ms["phi3"]["expr"], #element=FE["phi"],
                            degree=FE["phi"].degree()+degrise,
                            cell=FE["phi"].cell(),
                            t=0.0, **ms["phi3"]["prms"])
    es["v1"] = Expression(ms["v1"]["expr"], #element=FE["v"],
                          degree=FE["v"].degree()+degrise,
                          cell=FE["v"].cell(),
                          t=0.0, **ms["v1"]["prms"])
    es["v2"] = Expression(ms["v2"]["expr"], #element=FE["v"],
                          degree=FE["v"].degree()+degrise,
                          cell=FE["v"].cell(),
                          t=0.0, **ms["v2"]["prms"])
    es["v"] = Expression((ms["v1"]["expr"], ms["v2"]["expr"]),
                          #element=VectorElement(FE["v"], dim=2),
                          degree=FE["v"].degree()+degrise,
                          cell=FE["v"].cell(),
                          t=0.0, **ms["v2"]["prms"])
    es["p"] = Expression(ms["p"]["expr"], #element=FE["p"],
                         degree=FE["p"].degree()+degrise,
                         cell=FE["p"].cell(),
                         t=0.0, **ms["p"]["prms"])

    return es

def create_initial_conditions(ms):
    ic = SimpleCppIC()
    ic.add("phi", ms["phi1"]["expr"], t=0.0, **ms["phi1"]["prms"])
    ic.add("phi", ms["phi2"]["expr"], t=0.0, **ms["phi2"]["prms"])
    ic.add("phi", ms["phi3"]["expr"], t=0.0, **ms["phi3"]["prms"])
    ic.add("v",   ms["v1"]["expr"],   t=0.0, **ms["v1"]["prms"])
    ic.add("v",   ms["v2"]["expr"],   t=0.0, **ms["v2"]["prms"])
    ic.add("p",   ms["p"]["expr"],    t=0.0, **ms["p"]["prms"])

    return ic

def create_bcs(DS, boundary_markers, esol):
    bcs_v1 = DirichletBC(DS.subspace("v", 0), esol["v1"], boundary_markers, 0)
    bcs_v2 = DirichletBC(DS.subspace("v", 1), esol["v2"], boundary_markers, 0)
    bcs = {}
    bcs["v"] = [(bcs_v1, bcs_v2),]

    bcs["pcd"] = []

    return bcs

def create_source_terms(t_src, mesh, model, msol, matching_p):
    # Space and time variables
    # NOTE: Time variable must be named 't' because of 't' used in the string
    #       representations stored in 'msol[<var>]["expr"]'
    x = SpatialCoordinate(mesh)
    t = variable(t_src)

    DS = model.discretization_scheme()
    cell = DS.mesh().ufl_cell()

    # Manufactured solution
    A0 = Constant(msol["v1"]["prms"]["A0"], cell=cell, name="A0")
    a0 = Constant(msol["v1"]["prms"]["a0"], cell=cell, name="a0")
    b0 = Constant(msol["v1"]["prms"]["b0"], cell=cell, name="b0")
    w0 = Constant(msol["v1"]["prms"]["w0"], cell=cell, name="w0")

    A1 = Constant(msol["phi1"]["prms"]["A1"], cell=cell, name="A1")
    a1 = Constant(msol["phi1"]["prms"]["a1"], cell=cell, name="a1")
    b1 = Constant(msol["phi1"]["prms"]["b1"], cell=cell, name="b1")
    w1 = Constant(msol["phi1"]["prms"]["w1"], cell=cell, name="w1")

    A2 = Constant(msol["phi2"]["prms"]["A2"], cell=cell, name="A2")
    a2 = Constant(msol["phi2"]["prms"]["a2"], cell=cell, name="a2")
    b2 = Constant(msol["phi2"]["prms"]["b2"], cell=cell, name="b2")
    w2 = Constant(msol["phi2"]["prms"]["w2"], cell=cell, name="w2")

    A3 = Constant(msol["phi3"]["prms"]["A3"], cell=cell, name="A3")
    a3 = Constant(msol["phi3"]["prms"]["a3"], cell=cell, name="a3")
    b3 = Constant(msol["phi3"]["prms"]["b3"], cell=cell, name="b3")
    w3 = Constant(msol["phi3"]["prms"]["w3"], cell=cell, name="w3")

    phi1 = eval(msol["phi1"]["expr"])
    phi2 = eval(msol["phi2"]["expr"])
    phi3 = eval(msol["phi3"]["expr"])
    v1   = eval(msol["v1"]["expr"])
    v2   = eval(msol["v2"]["expr"])
    p    = eval(msol["p"]["expr"])

    phi = as_vector([phi1, phi2, phi3])
    v   = as_vector([v1, v2])

    # Intermediate manipulations
    # -- create multi-well potential and its derivative
    prm = model.parameters
    S, LA, iLA = model.build_stension_matrices(prefix="MS_")
    dw = DoublewellFactory.create(prm["doublewell"])
    varphi = variable(phi)
    F = multiwell(dw, varphi, S)
    dF = diff(F, varphi)
    # -- initialize mobility
    M0 = Constant(prm["mobility"]["M0"], cell=cell, name="MS_M0")
    m = Constant(prm["mobility"]["m"], cell=cell, name="MS_m")
    beta = Constant(1.0, cell=cell, name="MS_beta")
    Mo = model.mobility(M0, phi, phi, m, beta)
    # -- initialize constant coefficients
    THETA2 = Constant(prm["THETA2"], cell=cell, name="MS_THETA2")
    eps = Constant(prm["eps"], cell=cell, name="MS_eps")
    a, b = dw.free_energy_coefficents()
    a = Constant(a, cell=cell, name="MS_a")
    b = Constant(b, cell=cell, name="MS_b")
    # -- define chemical potential
    chi = (b/eps)*dot(iLA, dF) - 0.5*a*eps*div(grad(phi))
    # -- define interpolated density and viscosity
    rho_mat = model.collect_material_params("rho")
    nu_mat = model.collect_material_params("nu")
    rho = model.density(rho_mat, phi)
    nu = model.viscosity(nu_mat, phi)
    # -- define total flux
    J = total_flux(Mo, rho_mat, chi)
    # -- define capillary force
    f_cap = capillary_force(phi, chi, LA) # --> leads to "monolithic pressure"
    if not matching_p:
        if DS.name() == "FullyDecoupled":
            f_cap = - 0.5*a*eps*dot(grad(phi).T, dot(LA, div(grad(phi))))
        elif DS.name() == "SemiDecoupled":
            f_cap = - dot(grad(chi).T, dot(LA.T, phi))

    # Source term for CH part
    g_src = diff(phi, t) + div(outer(phi, v)) - div(Mo*grad(chi))
    #g_src = diff(phi, t) + dot(grad(phi), v) - div(Mo*grad(chi))

    # Source term for NS part
    f_src = (1.0/rho)*(
          rho*diff(v, t)
        + dot(grad(v), rho*v + THETA2*J)
        + grad(p)
        - div(2*nu*sym(grad(v)))
        - f_cap
    )

    if DS.name() == "Monolithic":
        # NOTE:
        #   This scheme takes into account the momentum balance in the
        #   conservative form, while 'f_src' above is based on the
        #   non-conservative form. Both forms are equivalent supposing that the
        #   balance of mass holds. The manufactured solution defined above
        #   however do not satisfy the mass balance, therefore we need to add
        #   the following terms to 'f_src'.
        f_src += (1.0/rho)*(v*diff(rho, t) + v*div(rho*v + THETA2*J))
    elif DS.name() == "SemiDecoupled":
        # NOTE:
        #   This scheme takes into account the momentum balance in the
        #   special form with square roots of rho. Again, 'f_src' above is
        #   based on the non-conservative form. In spite of reasoning
        #   mentioned in the previous comment, we need to add the following
        #   terms to 'f_src'.
        f_src += 0.5*(1.0/rho)*(v*diff(rho, t) + v*div(rho*v + THETA2*J))
        # NOTE:
        #   Artificial modification of source terms due to stabilization
        #   terms used in this specific time discretization scheme
        # domain_vol = DS.compute_domain_size()
        # alpha = as_vector([Constant(assemble(phi[i]*dx)/domain_vol) for i in range(len(phi))])
        # f_src -= (1.0/rho)*dot(grad(chi).T, dot(LA.T, alpha))
        # g_src -= div(outer(alpha, v))

    # FIXME: Remove this ugly hack!
    # Uncomment the following block of code to visualize balance of mass
    # bm = diff(rho, t) + div(rho*v + THETA2*J)
    # t_src.assign(Constant(0.1))
    # pyplot.figure(); domain=plot(bm, DS.mesh(), mode="warp")
    # pyplot.show()
    # exit()

    return f_src, g_src

def create_pcd_solver(comm, pcd_variant, ls, mumps_debug=False):
    prefix = ""

    # Set up linear solver (GMRES with right preconditioning using Schur fact)
    linear_solver = PCDKrylovSolver(comm=comm)
    linear_solver.set_options_prefix(prefix)
    linear_solver.parameters["relative_tolerance"] = 1e-6
    PETScOptions.set(prefix+"ksp_gmres_restart", 150)

    # Set up subsolvers
    PETScOptions.set(prefix+"fieldsplit_p_pc_python_type", "fenapack.PCDRPC_" + pcd_variant)
    if ls == "iterative":
        PETScOptions.set(prefix+"fieldsplit_u_ksp_type", "richardson")
        PETScOptions.set(prefix+"fieldsplit_u_ksp_max_it", 1)
        PETScOptions.set(prefix+"fieldsplit_u_pc_type", "hypre")
        PETScOptions.set(prefix+"fieldsplit_u_pc_hypre_type", "boomeramg")

        PETScOptions.set(prefix+"fieldsplit_p_PCD_Rp_ksp_type", "richardson")
        PETScOptions.set(prefix+"fieldsplit_p_PCD_Rp_ksp_max_it", 1)
        PETScOptions.set(prefix+"fieldsplit_p_PCD_Rp_pc_type", "hypre")
        PETScOptions.set(prefix+"fieldsplit_p_PCD_Rp_pc_hypre_type", "boomeramg")

        PETScOptions.set(prefix+"fieldsplit_p_PCD_Ap_ksp_type", "richardson")
        PETScOptions.set(prefix+"fieldsplit_p_PCD_Ap_ksp_max_it", 1)
        PETScOptions.set(prefix+"fieldsplit_p_PCD_Ap_pc_type", "hypre")
        PETScOptions.set(prefix+"fieldsplit_p_PCD_Ap_pc_hypre_type", "boomeramg")

        PETScOptions.set(prefix+"fieldsplit_p_PCD_Mp_ksp_type", "chebyshev")
        PETScOptions.set(prefix+"fieldsplit_p_PCD_Mp_ksp_max_it", 5)
        PETScOptions.set(prefix+"fieldsplit_p_PCD_Mp_ksp_chebyshev_eigenvalues", "0.5, 2.0")
        # CHANGE #0: The above estimate is valid only for P1 pressure.
        #            Increase upper bound for higher OPA!
        PETScOptions.set(prefix+"fieldsplit_p_PCD_Mp_pc_type", "jacobi")
    elif ls == "direct":
        # Debugging MUMPS
        if mumps_debug:
            PETScOptions.set(prefix+"fieldsplit_u_mat_mumps_icntl_4", 2)
            PETScOptions.set(prefix+"fieldsplit_p_PCD_Ap_mat_mumps_icntl_4", 2)
            PETScOptions.set(prefix+"fieldsplit_p_PCD_Mp_mat_mumps_icntl_4", 2)
    else:
        assert False

    # Apply options
    linear_solver.set_from_options()

    return linear_solver

def prepare_hook(t_src, model, esol, degrise, err):

    class TailoredHook(TSHook):

        def head(self, t, it, logger):
            self.t_src[0].assign(Constant(t))    # update source terms @ CTL
            for key in six.iterkeys(self.esol):
                self.esol[key].t = t # update exact solution (including bcs)
        def tail(self, t, it, logger):
            self.t_src[-1].assign(Constant(t))   # update source terms @ PTL
            esol = self.esol
            degrise = self.degrise
            pv = self.model.discretization_scheme().primitive_vars_ctl()
            phi, chi, v, p = pv["phi"], pv["chi"], pv["v"], pv["p"]
            phi_ = phi.split()
            chi_ = chi.split()
            v_ = v.split()
            p = p.dolfin_repr()
            # Error computations
            err = self.err
            err["p"] = errornorm(esol["p"], p, norm_type="L2",
                                 degree_rise=degrise)
            for (i, var) in enumerate(["phi1", "phi2", "phi3"]):
                err[var] = errornorm(esol[var], phi_[i], norm_type="L2",
                                     degree_rise=degrise)
            for (i, var) in enumerate(["v1", "v2"]):
                err[var] = errornorm(esol[var], v_[i], norm_type="L2",
                                     degree_rise=degrise)
            # Logging and reporting
            info("")
            begin("Errors in L^2 norm:")
            for (key, val) in six.iteritems(err):
                desc = "||{:4s} - {:>4s}_h|| = %g".format(key, key)
                logger.info(desc, (val,), ("err_"+key,), t)
            end()
            info("")

    return TailoredHook(t_src=t_src, model=model, esol=esol,
                        degrise=degrise, err=err)

@pytest.mark.parametrize("matching_p", [False,])
@pytest.mark.parametrize("pcd_variant", ["BRM1", ]) # "BRM2"
@pytest.mark.parametrize("ls", ["direct",])
def test_scaling_mesh(pcd_variant, ls, matching_p, postprocessor):
    """
    Compute convergence rates for fixed time step and gradually refined mesh or
    increasing element order.
    """
    set_log_level(WARNING)

    degrise = 3 # degree rise for computation of errornorm

    # Read parameters
    scriptdir = os.path.dirname(os.path.realpath(__file__))
    prm_file = os.path.join(scriptdir, "mms-parameters.xml")
    mpset.read(prm_file)

    # Fixed parameters
    OTD = postprocessor.OTD
    dt = postprocessor.dt
    t_end = postprocessor.t_end
    test_type = postprocessor.test

    # Discretization
    scheme = "SemiDecoupled"

    # Names and directories
    basename = postprocessor.basename
    outdir = postprocessor.outdir

    # Mesh independent predefined quantities
    msol = create_manufactured_solution()
    ic = create_initial_conditions(msol)

    # Iterate over refinement level
    for it in range(1, 6): # CHANGE #1: set "it in range(1, 8)"
        # Decide which test to perform
        if test_type == "ref":
            level = it
            k = 1
        elif test_type == "ord":
            level = 1
            k = it
        label = "{}_{}_level_{}_k_{}_{}".format(pcd_variant, ls, level, k, basename)
        with Timer("Prepare") as tmr_prepare:
            # Prepare discretization
            mesh, boundary_markers = create_domain(level)
            DS = create_discretization(scheme, mesh, k)
            DS.parameters["PTL"] = OTD
            DS.setup()
            DS.load_ic_from_simple_cpp(ic)
            esol = create_exact_solution(msol, DS.finite_elements(), degrise)
            bcs = create_bcs(DS, boundary_markers, esol)

            # Prepare model
            model = ModelFactory.create("Incompressible", DS, bcs)
            cell = DS.mesh().ufl_cell()
            t_src_ctl = Constant(0.0, cell=cell, name="t_src_ctl")
            t_src_ptl = Constant(0.0, cell=cell, name="t_src_ptl")
            f_src_ctl, g_src_ctl = \
              create_source_terms(t_src_ctl, mesh, model, msol, matching_p)
            f_src_ptl, g_src_ptl = \
              create_source_terms(t_src_ptl, mesh, model, msol, matching_p)
            # NOTE: Source terms are time-dependent. Updates to these terms
            #       are possible via 't_src.assign(Constant(t))', where 't'
            #       denotes the actual time value.
            t_src = [t_src_ctl,]
            f_src = [f_src_ctl,]
            g_src = [g_src_ctl,]
            if OTD == 2:
                t_src.append(t_src_ptl)
                g_src.append(g_src_ptl)
            model.load_sources(f_src, g_src)
            forms = model.create_forms(matching_p)

            # NOTE: Here is the possibility to modify forms, e.g. by adding
            #       boundary integrals.

            # Prepare solver
            comm = mesh.mpi_comm()
            solver = SolverFactory.create(model, forms, fix_p=True)
            solver.data["solver"]["NS"] = \
              create_pcd_solver(comm, pcd_variant, ls, mumps_debug=False)

            # PETScOptions.set("ksp_monitor")
            # solver.data["solver"]["NS"].set_from_options()

            # Prepare time-stepping algorithm
            xfields = None
            # NOTE: Uncomment the following block of code to get XDMF output
            # pv = DS.primitive_vars_ctl()
            # phi, chi, v, p = pv["phi"], pv["chi"], pv["v"], pv["p"]
            # phi_, chi_, v_ = phi.split(), chi.split(), v.split()
            # xfields = list(zip(phi_, len(phi_)*[None,])) \
            #         + list(zip(v_, len(v_)*[None,])) \
            #         + [(p.dolfin_repr(), None),]
            hook = prepare_hook(t_src, model, esol, degrise, {})
            logfile = "log_{}.dat".format(label)
            #info("BREAK POINT %ia" % level)
            TS = TimeSteppingFactory.create("ConstantTimeStep", comm, solver,
                   hook=hook, logfile=logfile, xfields=xfields, outdir=outdir)
            #info("BREAK POINT %ib" % level) <-- not reached for level == 2
            #                                    when running in parallel

        # Time-stepping
        t_beg = 0.0
        with Timer("Time stepping") as tmr_tstepping:
            result = TS.run(t_beg, t_end, dt, OTD)

        # Get number of Krylov iterations if relevant
        try:
            krylov_it = solver.iters["NS"][0]
        except AttributeError:
            krylov_it = 0

        # Prepare results
        name = logfile[4:-4]
        x_var = k if test_type == "ord" else mesh.hmin()
        result.update(
            scheme=scheme,
            pcd_variant=pcd_variant,
            ls=ls,
            krylov_it=krylov_it,
            ndofs=DS.num_dofs(),
            dt=dt,
            t_end=t_end,
            OTD=OTD,
            err=hook.err,
            x_var=x_var,
            tmr_prepare=tmr_prepare.elapsed()[0],
            tmr_tstepping=tmr_tstepping.elapsed()[0]
        )
        print(name, result["ndofs"], result["it"],
                  result["krylov_it"], result["tmr_tstepping"])

        # Send to posprocessor
        rank = MPI.rank(comm)
        postprocessor.add_result(rank, result)

        # Save results into a binary file
        filename = "results_{}.pickle".format(label)
        postprocessor.save_results(filename)

    # Pop results that we do not want to report at the moment
    postprocessor.pop_items(["ndofs", "scheme", "tmr_prepare"])

    # Flush plots as we now have data for all level values
    postprocessor.flush_plots()

    # Store timings
    #datafile = os.path.join(outdir, "timings.xml")
    #dump_timings_to_xml(datafile, TimingClear_clear)

    # Cleanup
    set_log_level(INFO)
    #mpset.write(comm, prm_file) # uncomment to save parameters
    mpset.refresh()
    gc.collect()

@pytest.fixture(scope='module')
def postprocessor(request):
    dt = 0.001
    t_end = 0.01 # CHANGE #2: set "t_end = 0.1"
    OTD = 1 # CHANGE #3: set "OTD" to 1 or 2 according to chosen scheme
    test = 1 # 0 ... order, 1 ... refinement
    rank = MPI.rank(mpi_comm_world())
    scriptdir = os.path.dirname(os.path.realpath(__file__))
    outdir = os.path.join(scriptdir, __name__)
    proc = Postprocessor(dt, t_end, OTD, test, outdir)

    # Decide what should be plotted
    proc.register_fixed_variables((("dt", dt), ("t_end", t_end), ("OTD", OTD)))

    # Dump empty postprocessor into a file for later use
    filename = "proc_{}.pickle".format(proc.basename)
    proc.dump_to_file(rank, filename)

    # Create plots if plotting is enabled otherwise do nothing
    if not os.environ.get("DOLFIN_NOPLOT"):
        proc.create_plots(rank)
        #pyplot.show(); exit() # uncomment to explore current layout of plots

    def fin():
        print("teardown postprocessor")
        proc.save_results("results_saved_at_teardown.pickle")
    request.addfinalizer(fin)
    return proc

class Postprocessor(GenericBenchPostprocessor):
    def __init__(self, dt, t_end, OTD, test, outdir, averaging=True):
        super(Postprocessor, self).__init__(outdir)

        # Determine test_type
        _test_type = ["ord", "ref"]

        # Hack enabling change of fixed variables at one place
        self.dt = dt
        self.t_end = t_end
        self.OTD = OTD
        self.test = _test_type[test]

        # So far hardcoded values
        self.x_var = "x_var"
        self.y_var0 = "err"
        self.y_var1 = "tmr_tstepping"
        self.y_var2 = "krylov_it"
        self.y_var3 = "tmr_solve"

        # Store names
        self.basename = "{}_dt_{}_t_end_{}_OTD_{}".format(self.test, dt, t_end, OTD)

        # Store other options
        self._avg = averaging

    def flush_plots(self):
        if not self.plots:
            self.results = []
            return
        coord_vars = (self.x_var, self.y_var0, self.y_var1,
                      self.y_var2, self.y_var3)
        for fixed_vars, fig in six.iteritems(self.plots):
            fixed_var_names = next(six.moves.zip(*fixed_vars))
            data = {}
            #styles = {"Monolithic": '.--', "SemiDecoupled": '+--', "FullyDecoupled": 'x--'}
            for result in self.results:
                style = '+--' #styles[result["scheme"]]
                if not all(result[name] == value for name, value in fixed_vars):
                    continue
                free_vars = tuple((var, val) for var, val in six.iteritems(result)
                                  if var not in coord_vars
                                  and var not in fixed_var_names)
                datapoints = data.setdefault(free_vars, {})
                # NOTE: Variable 'datapoints' is now a "pointer" to an empty
                #       dict that is stored inside 'data' under key 'free_vars'
                xs = datapoints.setdefault("xs", [])
                ys0 = datapoints.setdefault("ys0", [])
                ys1 = datapoints.setdefault("ys1", [])
                ys2 = datapoints.setdefault("ys2", [])
                ys3 = datapoints.setdefault("ys3", [])
                xs.append(result[self.x_var])
                ys0.append(result[self.y_var0])
                ys1.append(result[self.y_var1])
                N = float(result["it"]) if self._avg else 1
                ys2.append(result[self.y_var2]/N)
                ys3.append(result[self.y_var3]/N)

            for free_vars, datapoints in six.iteritems(data):
                xs = datapoints["xs"]
                ys0 = datapoints["ys0"]
                ys1 = datapoints["ys1"]
                ys2 = datapoints["ys2"]
                ys3 = datapoints["ys3"]
                self._plot(fig, xs, ys0, ys1, ys2, ys3, free_vars, style)
            self._save_plot(fig, fixed_vars, self.outdir, self.test+"_")
        self.results = []

    @staticmethod
    def _plot(fig, xs, ys0, ys1, ys2, ys3, free_vars, style):
        (fig1, fig2, fig3, fig4), (ax1, ax2, ax3, ax4) = fig
        label = "_".join(map(str, itertools.chain(*free_vars)))
        for (i, var) in enumerate(["phi1", "phi2", "phi3"]):
            ax1.plot(xs, [d[var] for d in ys0], style, linewidth=0.2,
                     label=r"$L^2$-$\phi_{}$".format(i+1))
        for (i, var) in enumerate(["v1", "v2"]):
            ax1.plot(xs, [d[var] for d in ys0], style, linewidth=0.2,
                     label=r"$L^2$-$v_{}$".format(i+1))
        ax1.plot(xs, [d["p"] for d in ys0], style, linewidth=0.2,
                 label=r"$L^2$-$p$")
        ax2.plot(xs, ys1, '*--', linewidth=0.2, label=label)
        ax3.plot(xs, ys2, '+--', linewidth=0.2, label=label)
        ax4.plot(xs, ys3, '+--', linewidth=0.2, label=label)
        # ax1.legend(bbox_to_anchor=(1.01, 1), loc=2, borderaxespad=0,
        #            fontsize='x-small', ncol=1)
        # ax2.legend(bbox_to_anchor=(0, 1.1), loc=2, borderaxespad=0,
        #            fontsize='x-small', ncol=3)
        ax1.legend(loc=4, borderaxespad=1, fontsize='x-small', ncol=1)
        ax2.legend(loc=1, borderaxespad=1, fontsize='x-small', ncol=3)
        ax3.legend(loc=0, borderaxespad=1, fontsize='x-small', ncol=3)
        ax4.legend(loc=0, borderaxespad=1, fontsize='x-small', ncol=3)

    @staticmethod
    def _save_plot(fig, fixed_vars, outdir="", prefix=""):
        subfigs, (ax1, ax2, ax3, ax4) = fig
        filename = "_".join(map(str, itertools.chain(*fixed_vars)))
        import matplotlib.backends.backend_pdf
        pdf = matplotlib.backends.backend_pdf.PdfPages(
                  os.path.join(outdir, "fig_" + prefix + filename + ".pdf"))
        for fig in subfigs:
            pdf.savefig(fig)
        pdf.close()

    def _create_figure(self):
        fig1, fig2 = pyplot.figure(), pyplot.figure()
        gs = gridspec.GridSpec(2, 2, width_ratios=[1, 0.01],
                               height_ratios=[10, 1], hspace=0.1)
        # Set subplots
        ax1 = fig1.add_subplot(gs[0, 0])
        ax2 = fig2.add_subplot(gs[0, 0], sharex=ax1)
        #ax1.xaxis.set_label_position("top")
        #ax1.xaxis.set_ticks_position("top")
        #ax1.xaxis.set_tick_params(labeltop="on", labelbottom="off")
        #pyplot.setp(ax2.get_xticklabels(), visible=False)

        fig3, fig4 = pyplot.figure(), pyplot.figure()
        gs = gridspec.GridSpec(2, 2, height_ratios=[3, 1], width_ratios=[0.01, 1], hspace=0.05)
        # Set subplots
        ax3 = fig3.add_subplot(gs[0, 1])
        ax4 = fig4.add_subplot(gs[0, 1], sharex=ax3)

        # Set scales
        if self.test == "ref":
            ax1.set_xscale("log")
            ax2.set_xscale("log")
        if self.test == "ord":
            from matplotlib.ticker import MaxNLocator
            ax1.xaxis.set_major_locator(MaxNLocator(integer=True))
        ax1.set_yscale("log")
        ax2.set_yscale("log")
        # Set ticks
        for ax in [ax1, ax2, ax3, ax4]:
            ax.tick_params(which="both", direction="in", right=True, top=True)
        # Set labels
        xlabel = "$h$" if self.test == "ref" else "Element order"
        ax1.set_xlabel(xlabel)
        ax2.set_xlabel(ax1.get_xlabel())
        ax1.set_ylabel("$L^2$ errors")
        ax2.set_ylabel("CPU time")
        ax1.set_ylim(None, None, auto=True)
        ax2.set_ylim(None, None, auto=True)

        tail = "[p.t.s.]" if self._avg else ""
        ax3.set_xscale('log')
        ax4.set_xscale('log')
        ax4.set_yscale('log')
        ax3.set_xlabel(ax1.get_xlabel())
        ax4.set_xlabel(ax1.get_xlabel())
        ax3.set_ylabel("# GMRES iterations {}".format(tail))
        ax4.set_ylabel("CPU time {}".format(tail))
        ax3.set_ylim(None, None, auto=True)
        ax4.set_ylim(None, None, auto=True)

        return (fig1, fig2, fig3, fig4), (ax1, ax2, ax3, ax4)
