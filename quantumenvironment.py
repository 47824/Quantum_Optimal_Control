"""
Class to generate a RL environment suitable for usage with TF-agents, leveraging Qiskit modules to simulate
quantum system (could also include QUA code in the future)

Author: Arthur Strauss
Created on 28/11/2022
"""

# For compatibility for options formatting between Estimators.
import json
from itertools import product, permutations
from typing import Dict, Union, Optional, List, Callable

import numpy as np
# Qiskit imports
from qiskit import pulse, schedule
from qiskit.circuit import Parameter, QuantumCircuit, QuantumRegister, Gate, CircuitInstruction
from qiskit.circuit.library.standard_gates import get_standard_gate_name_mapping
# Qiskit Estimator Primitives: for computing Pauli expectation value sampling easily
from qiskit.primitives import BaseEstimator, Estimator, BackendEstimator
# Qiskit backends
from qiskit.providers import BackendV1, BackendV2
from qiskit.providers.fake_provider.fake_backend import FakeBackend, FakeBackendV2
from qiskit.pulse.transforms.canonicalization import block_to_schedule
# Qiskit Quantum Information, for fidelity benchmarking
from qiskit.quantum_info.operators import SparsePauliOp, Operator, pauli_basis
from qiskit.quantum_info.operators.measures import average_gate_fidelity, state_fidelity, process_fidelity
from qiskit.quantum_info.states import DensityMatrix, Statevector
from qiskit.transpiler import InstructionProperties, Layout
from qiskit_aer.backends.aerbackend import AerBackend
from qiskit_aer.primitives import Estimator as Aer_Estimator
# Qiskit dynamics for pulse simulation (& benchmarking)
from qiskit_dynamics import DynamicsBackend, Solver
from qiskit_dynamics.array import Array, wrap
from qiskit_dynamics.models import HamiltonianModel
# Qiskit Experiments for generating reliable baseline for complex gate calibrations / state preparations
from qiskit_experiments.calibration_management.calibrations import Calibrations
from qiskit_experiments.framework import BatchExperiment
from qiskit_experiments.library import ProcessTomography, RoughXSXAmplitudeCal, RoughDragCal
from qiskit_experiments.library.tomography.basis import PauliPreparationBasis  # , Pauli6PreparationBasis
from qiskit_ibm_runtime import Estimator as Runtime_Estimator, IBMBackend as Runtime_Backend, Options, Session
# Tensorflow modules
from tensorflow_probability.python.distributions import Categorical

from qconfig import QiskitConfig
# from qiskit_experiments.calibration_management.basis_gate_library import FixedFrequencyTransmon, EchoedCrossResonance
from torch_gate_calibration.basis_gate_library import FixedFrequencyTransmon, EchoedCrossResonance

# QUA imports
# from qualang_tools.bakery.bakery import baking
# from qm.qua import *
# from qm.QuantumMachinesManager import QuantumMachinesManager

Estimator_type = Union[Aer_Estimator, Runtime_Estimator, Estimator, BackendEstimator]
Backend_type = Union[BackendV1, BackendV2]

def perform_standard_calibrations(backend: DynamicsBackend, calibration_files: Optional[List[str]] = None):
    """
    Generate baseline single qubit gates (X, SX, RZ, H) for all qubits using traditional calibration experiments
    :param backend: Dynamics Backend on which calibrations should be run
    :param calibration_files: Optional calibration files containing single qubit gate calibrations for provided
        DynamicsBackend instance (Qiskit Experiments does not support this feature yet)

    """

    target, qubits = backend.target, []
    for i, dim in enumerate(backend.options.subsystem_dims):
        if dim > 1:
            qubits.append(i)
    num_qubits = len(qubits)
    single_qubit_properties = {(qubit,): None for qubit in range(num_qubits)}
    single_qubit_errors = {(qubit,): 0.0 for qubit in qubits}

    control_channel_map = backend.options.control_channel_map or {(qubits[0], qubits[1]): index
                                                                  for index, qubits in
                                                                  enumerate(tuple(permutations(qubits, 2)))}
    if backend.options.control_channel_map:
        physical_control_channel_map = {(qubit_pair[0], qubit_pair[1]): backend.control_channel((qubit_pair[0],
                                                                                                 qubit_pair[1]))
                                        for qubit_pair in backend.options.control_channel_map}
    else:
        physical_control_channel_map = {(qubit_pair[0], qubit_pair[1]): [pulse.ControlChannel(index)]
                                        for index, qubit_pair in enumerate(tuple(permutations(qubits, 2)))}
    backend.set_options(control_channel_map=control_channel_map)
    coupling_map = [list(qubit_pair) for qubit_pair in control_channel_map]
    two_qubit_properties = {qubits: None for qubits in control_channel_map}
    two_qubit_errors = {qubits: 0.0 for qubits in control_channel_map}
    standard_gates: Dict[str, Gate] = get_standard_gate_name_mapping()  # standard gate library
    fixed_phase_gates, fixed_phases = ["z", "s", "sdg", "t", "tdg"], np.pi * np.array([1, 1 / 2, -1 / 2, 1 / 4, -1 / 4])
    other_gates = ["rz", "id", "h", "x", "sx", "reset"]
    single_qubit_gates = fixed_phase_gates + other_gates
    two_qubit_gates = ["ecr"]
    exp_results = {}
    existing_cals = calibration_files is not None

    phi: Parameter = standard_gates["rz"].params[0]
    if existing_cals:
        cals = Calibrations.load(files=calibration_files)
    else:
        cals = Calibrations(coupling_map=coupling_map, control_channel_map=physical_control_channel_map,
                            libraries=[FixedFrequencyTransmon(basis_gates=["x", "sx"]),
                                       EchoedCrossResonance(basis_gates=['cr45p', 'cr45m', 'ecr'])],
                            backend_name=backend.name, backend_version=backend.backend_version)
    if len(target.instruction_schedule_map().instructions) <= 1:  # Check if instructions have already been added
        for gate in single_qubit_gates:
            target.add_instruction(standard_gates[gate], properties=single_qubit_properties)
        if num_qubits > 1:
            for gate in two_qubit_gates:
                target.add_instruction(standard_gates[gate], properties=two_qubit_properties)
            target.build_coupling_map(two_q_gate=two_qubit_gates[0])

    for qubit in qubits:  # Add calibrations for each qubit
        control_channels = list(filter(lambda x: x is not None, [control_channel_map.get((i, qubit), None)
                                                                 for i in qubits]))
        # Calibration of RZ gate, virtual Z-rotation
        with pulse.build(backend, name=f"rz{qubit}") as rz_cal:
            pulse.shift_phase(-phi, pulse.DriveChannel(qubit))
            for q in control_channels:
                pulse.shift_phase(-phi, pulse.ControlChannel(q))
        # Identity gate
        id_cal = pulse.Schedule(pulse.Delay(20, pulse.DriveChannel(qubit)))  # Wait 20 cycles for identity gate

        # Update backend Target by adding calibrations for all phase gates (fixed angle virtual Z-rotations)
        target.update_instruction_properties('rz', (qubit,), InstructionProperties(calibration=rz_cal, error=0.))
        target.update_instruction_properties('id', (qubit,), InstructionProperties(calibration=id_cal, error=0.))
        target.update_instruction_properties("reset", (qubit,), InstructionProperties(calibration=id_cal, error=0.))
        for phase, gate in zip(fixed_phases, fixed_phase_gates):
            gate_cal = rz_cal.assign_parameters({phi: phase}, inplace=False)
            instruction_prop = InstructionProperties(calibration=gate_cal, error=0.)
            target.update_instruction_properties(gate, (qubit,), instruction_prop)

        # Perform calibration experiments (Rabi/Drag) for calibrating X and SX gates
        if not existing_cals:
            rabi_exp = RoughXSXAmplitudeCal([qubit], cals, backend=backend, amplitudes=np.linspace(-0.2, 0.2, 100))
            drag_exp = RoughDragCal([qubit], cals, backend=backend, betas=np.linspace(-20, 20, 15))
            drag_exp.set_experiment_options(reps=[3, 5, 7])
            print(f"Starting Rabi experiment for qubit {qubit}...")
            rabi_result = rabi_exp.run().block_for_results()
            print(f"Rabi experiment for qubit {qubit} done.")
            print(f"Starting Drag experiment for qubit {qubit}...")
            drag_result = drag_exp.run().block_for_results()
            print(f"Drag experiments done for qubit {qubit} done.")
            exp_results[qubit] = [rabi_result, drag_result]

        # Build Hadamard gate schedule from following equivalence: H = S @ SX @ S

        sx_schedule = block_to_schedule(cals.get_schedule("sx", (qubit,)))
        s_schedule = block_to_schedule(target.get_calibration('s', (qubit,)))
        h_schedule = pulse.Schedule(s_schedule, sx_schedule, s_schedule, name="h")
        target.update_instruction_properties('h', (qubit,), properties=InstructionProperties(calibration=h_schedule,
                                                                                             error=0.0))

    print("All single qubit calibrations are done")
    # cals.save(file_type="csv", overwrite=True, file_prefix="Custom" + backend.name)
    error_dict = {'x': single_qubit_errors, 'sx': single_qubit_errors}
    target.update_from_instruction_schedule_map(cals.get_inst_map(), error_dict=error_dict)
    print(control_channel_map)
    # for qubit_pair in control_channel_map:
    #     print(qubit_pair)
    #     cr_ham_exp = CrossResonanceHamiltonian(physical_qubits=qubit_pair, flat_top_widths=np.linspace(0, 5000, 17),
    #                                            backend=backend)
    #     print("Calibrating CR for qubits", qubit_pair, "...")
    #     data_cr = cr_ham_exp.run().block_for_results()
    #     exp_results[qubit_pair] = data_cr

    print("Updated Instruction Schedule Map", target.instruction_schedule_map())

    return cals, exp_results


def _calculate_chi_target_state(target_state: Dict, n_qubits: int):
    """
    Calculate for all P
    :param target_state: Dictionary containing info on target state (name, density matrix)
    :param n_qubits: Number of qubits
    :return: Target state supplemented with appropriate "Chi" key
    """
    assert 'dm' in target_state, 'No input data for target state, provide DensityMatrix'
    d = 2 ** n_qubits
    Pauli_basis = pauli_basis(num_qubits=n_qubits)
    target_state["Chi"] = np.array([np.trace(np.array(target_state["dm"].to_operator())
                                             @ Pauli_basis[k].to_matrix()).real
                                    for k in range(d ** 2)])
    # Real part is taken to convert it in good format,
    # but imaginary part is always 0. as dm is hermitian and Pauli is traceless
    return target_state


def _define_target(target: Dict):
    if "register" in target:
        assert isinstance(target["register"], (List, QuantumRegister)), "Register should be of type List[int] " \
                                                                        "or Quantum Register"
    tgt_register = target.get("register", None)
    if isinstance(tgt_register, List):
        q_register = QuantumRegister(len(tgt_register))
        layout = Layout({q_register[i]: tgt_register[i] for i in range(len(tgt_register))})
    else:  # QuantumRegister or None
        q_register = tgt_register
        layout = None

    if 'gate' not in target and 'circuit' not in target and 'dm' not in target:
        raise KeyError("No target provided, need to have one of the following: 'gate' for gate calibration,"
                       " 'circuit' or 'dm' for state preparation")
    if ('gate' in target and 'circuit' in target) or ('gate' in target and 'dm' in target):
        raise KeyError("Cannot have simultaneously a gate target and a state target")
    if "circuit" in target or "dm" in target:
        target["target_type"] = "state"
        if 'circuit' in target:
            assert isinstance(target["circuit"], QuantumCircuit), "Provided circuit is not a qiskit.QuantumCircuit " \
                                                                  "object"
            target["dm"] = DensityMatrix(target["circuit"])

        assert 'dm' in target, 'no DensityMatrix or circuit argument provided to target dictionary'
        assert isinstance(target["dm"], DensityMatrix), 'Provided dm is not a DensityMatrix object'
        dm: DensityMatrix = target["dm"]
        n_qubits = dm.num_qubits

        if q_register is None:
            q_register = QuantumRegister(n_qubits)

        if layout is None:
            layout = Layout.generate_trivial_layout(q_register)

        return _calculate_chi_target_state(target, n_qubits), "state", q_register, n_qubits, layout

    elif "gate" in target:
        target["target_type"] = "gate"
        assert isinstance(target["gate"], Gate), "Provided gate is not a qiskit.circuit.Gate operation"
        gate: Gate = target["gate"]
        n_qubits = target["gate"].num_qubits
        if q_register is None:
            q_register = QuantumRegister(n_qubits)
        if layout is None:
            layout = Layout.generate_trivial_layout(q_register)

        assert gate.num_qubits == len(q_register), f"Target gate number of qubits ({gate.num_qubits}) " \
                                                   f"incompatible with indicated 'register' ({len(tgt_register)})"
        if 'input_states' not in target:
            target['input_states'] = [{"circuit": PauliPreparationBasis().circuit(s).decompose()}
                                      for s in product(range(4), repeat=len(tgt_register))]

            # target['input_states'] = [{"dm": Pauli6PreparationBasis().matrix(s),
            #                            "circuit": CircuitOp(Pauli6PreparationBasis().circuit(s).decompose())}
            #                           for s in product(range(6), repeat=len(tgt_register))]

        for i, input_state in enumerate(target["input_states"]):
            if 'circuit' not in input_state:
                raise KeyError("'circuit' key missing in input_state")
            assert isinstance(input_state["circuit"], QuantumCircuit), "Provided circuit is not a" \
                                                                       "qiskit.QuantumCircuit object"

            input_circuit: QuantumCircuit = input_state['circuit']
            input_state['dm'] = DensityMatrix(input_circuit)

            state_target_circuit = QuantumCircuit(q_register)
            state_target_circuit.append(input_circuit.to_instruction(), q_register)
            state_target_circuit.append(CircuitInstruction(gate, q_register))

            input_state['target_state'] = {"dm": DensityMatrix(state_target_circuit),
                                           "circuit": state_target_circuit,
                                           "target_type": "state"}
            input_state['target_state'] = _calculate_chi_target_state(input_state['target_state'], n_qubits)
        return target, "gate", q_register, n_qubits, layout
    else:
        raise KeyError('target type not identified, must be either gate or state')


class QuantumEnvironment:

    def __init__(self, target: Dict, abstraction_level: str = 'circuit',
                 Qiskit_config: Optional[QiskitConfig] = None,
                 QUA_config: Optional[Dict] = None,
                 sampling_Pauli_space: int = 10, n_shots: int = 1, c_factor: float = 0.5):
        """
        Class for building quantum environment for RL agent aiming to perform a state preparation task.

        :param target: control target of interest (can be either a gate to be calibrated or a state to be prepared)
            Target should be a Dict containing target type ('gate' or 'state') as well as necessary fields to initiate
            the calibration (cf. example)
        :param abstraction_level: Circuit or pulse level parametrization of action space
        :param Qiskit_config: Dictionary containing all info for running Qiskit program (i.e. service, backend,
            options, parametrized_circuit)
        :param QUA_config: Dictionary containing all infor for running a QUA program
        :param sampling_Pauli_space: Number of samples to build fidelity estimator for one action
        :param n_shots: Number of shots to sample for one specific computation (action/Pauli expectation sampling)
        :param c_factor: Scaling factor for reward normalization
        """

        assert abstraction_level == 'circuit' or abstraction_level == 'pulse', 'Abstraction layer can be either pulse' \
                                                                               ' or circuit'
        self.abstraction_level: str = abstraction_level
        if Qiskit_config is None and QUA_config is None:
            raise AttributeError("QuantumEnvironment requires one software configuration (can be Qiskit or QUA based)")
        if Qiskit_config is not None and QUA_config is not None:
            raise AttributeError("Cannot provide simultaneously a QUA setup and a Qiskit config ")
        elif Qiskit_config is not None:
            self._config_type = "Qiskit"
            self._config: Qiskit_config = Qiskit_config
            self.target, self.target_type, self.tgt_register, self._n_qubits, self._layout = _define_target(target)

            self._d = 2 ** self.n_qubits
            self.c_factor = c_factor
            self.sampling_Pauli_space = sampling_Pauli_space
            self.n_shots = n_shots
            self.Pauli_ops = pauli_basis(num_qubits=self._n_qubits)

            self.backend: Optional[Backend_type] = Qiskit_config.backend
            self.parametrized_circuit_func: Callable = Qiskit_config.parametrized_circuit
            estimator_options: Optional[Union[Options, Dict]] = Qiskit_config.estimator_options

            if self.abstraction_level == "circuit":  # Either state-vector simulation (native) or AerBackend provided
                if isinstance(self.backend, (AerBackend, FakeBackend, FakeBackendV2)):
                    # Estimator taking noise model into consideration, have to provide an AerBackend
                    # TODO: Extract from TranspilationOptions a dict that can go in following definition
                    self._estimator: Estimator_type = Aer_Estimator(backend_options=self.backend.options,
                                                                   transpile_options={'initial_layout': self._layout},
                                                                   approximation=True)
                else:  # No backend specified, ideal state-vector simulation
                    self._estimator: Estimator_type = Estimator(options={"initial_layout": self._layout})

            else: # Pulse level abstraction
                if not isinstance(self.backend, (Runtime_Backend, DynamicsBackend)):
                    raise TypeError("Backend must be either DynamicsBackend or Qiskit Runtime backend if pulse level"
                                    "abstraction is selected (Aer Pulse simulator deprecated")


                if isinstance(self.backend, DynamicsBackend):
                    import jax
                    jit = wrap(jax.jit, decorator=True)
                    self._estimator: Estimator_type = BackendEstimator(self.backend, skip_transpilation= False)
                    self._estimator.set_transpile_options(initial_layout=self.layout)
                    if self.config.do_calibrations:
                        calibration_files: List[str] = Qiskit_config.calibration_files
                        self.calibrations, self.exp_results = perform_standard_calibrations(self.backend,
                                                                                            calibration_files)
                    self._benchmark_backend = self.backend
                else:
                    if hasattr(self.backend, "configuration"):
                        self._benchmark_backend = DynamicsBackend.from_backend(self.backend,
                                                                           subsystem_list=target["register"])
                # For benchmarking the gate at each epoch, set tools for Pulse level simulator
                if Qiskit_config.solver is not None:
                    self.solver: Solver = Qiskit_config.solver
                else:
                    self.solver: Solver = self._benchmark_backend.options.solver  # Custom Solver
                # Can describe noisy channels, if none provided, pick Solver associated to DynamicsBackend by default
                self.model_dim, self.channel_freq  = self.solver.model.dim, Qiskit_config.channel_freq
                if isinstance(self.solver.model, HamiltonianModel):
                    self.y_0 = Array(np.eye(self.model_dim))
                    self.ground_state = Array(np.array([1.0] + [0.0] * (self.model_dim - 1)))
                else:
                    self.y_0 = Array(np.eye(self.model_dim ** 2))
                    self.ground_state = Array(np.array([1.0] + [0.0] * (self.model_dim ** 2 - 1)))

            if isinstance(self.backend, Runtime_Backend):  # Real backend, or Simulation backend from Runtime Service
                self._estimator: Estimator_type = Runtime_Estimator(session=Session(self.backend.service, self.backend),
                                                                   options=estimator_options)
                if self.estimator.options.transpilation['initial_layout'] is None:
                    self.estimator.options.transpilation['initial_layout']=self._layout.get_physical_bits()

        elif QUA_config is not None:
            raise AttributeError("QUA compatibility not yet implemented")
            # self._config_type = "QUA"
            # self._config = QUA_config

            # TODO: Add a QUA program

        # Data storage for TF-Agents or plotting
        self._step_tracker = 0
        self.action_history = []
        self.density_matrix_history = []
        self.reward_history = []
        self.qc_history = []
        if self.target_type == 'gate':
            self.process_fidelity_history = []
            self.avg_fidelity_history = []
            self.built_unitaries = []
        else:
            self.state_fidelity_history = []

    def perform_action(self, actions: np.array, do_benchmark: bool = True):
        """
        Execute quantum circuit with parametrized amplitude, retrieve measurement result and assign rewards accordingly
        :param actions: action vector to execute on quantum system
        :param do_benchmark: Indicates if actual fidelity computation should be done on top of reward computation
        :return: Reward table (reward for each run in the batch)
        """

        qc = QuantumCircuit(self.tgt_register)  # Reset the QuantumCircuit instance for next iteration
        parametrized_circ = QuantumCircuit(self.tgt_register)
        angles, batch_size = np.array(actions), len(np.array(actions))
        self.action_history.append(angles)

        if self.target_type == 'gate':
            # Pick random input state from the list of possible input states (forming a tomographically complete set)
            index = np.random.randint(len(self.target["input_states"]))
            input_state = self.target["input_states"][index]
            target_state = input_state["target_state"]  # Ideal output state associated to input (Gate |input>=|output>)
            # Append input state circuit to full quantum circuit for gate calibration
            qc.append(input_state["circuit"].to_instruction(), self.tgt_register)

        else:  # State preparation task
            target_state = self.target
        # Direct fidelity estimation protocol  (https://doi.org/10.1103/PhysRevLett.106.230501)
        distribution = Categorical(probs=target_state["Chi"] ** 2)
        k_samples = distribution.sample(self.sampling_Pauli_space)
        pauli_index, pauli_shots = np.unique(k_samples, return_counts=True)
        reward_factor = np.round([self.c_factor * target_state["Chi"][p] / (self._d * distribution.prob(p))
                                  for p in pauli_index], 5)

        # Retrieve Pauli observables to sample, and build a weighted sum to feed the Estimator primitive
        observables = SparsePauliOp.from_list([(self.Pauli_ops[p].to_label(), reward_factor[i])
                                               for i, p in enumerate(pauli_index)])
        # Benchmarking block: not part of reward calculation
        # Apply parametrized quantum circuit (action), for benchmarking only

        self.parametrized_circuit_func(parametrized_circ)
        qc_list = [parametrized_circ.bind_parameters(angle_set) for angle_set in angles]
        self.qc_history.append(qc_list)
        if do_benchmark:
            self._store_benchmarks(qc_list)

        # Build full quantum circuit: concatenate input state prep and parametrized unitary
        self.parametrized_circuit_func(qc)
        if isinstance(self.estimator, Runtime_Estimator):
            job = self.estimator.run(circuits=[qc] * batch_size, observables=[observables] * batch_size,
                                 parameter_values=angles, shots=self.sampling_Pauli_space * self.n_shots,
                                 job_tags=[f"rl_qoc_step{self._step_tracker}"])
        else:
            job = self.estimator.run(circuits=[qc] * batch_size, observables=[observables] * batch_size,
                                     parameter_values=angles, shots=self.sampling_Pauli_space * self.n_shots)

        self._step_tracker += 1
        reward_table = job.result().values
        self.reward_history.append(reward_table)
        assert len(reward_table) == batch_size
        return reward_table # Shape [batchsize]

    def _store_benchmarks(self, qc_list: List[QuantumCircuit]):
        """
        Method to store in lists all relevant data to assess performance of training (fidelity information)
        """

        # Circuit list for each action of the batch
        if self.abstraction_level == 'circuit':

            q_state_list = [Statevector.from_instruction(qc) for qc in qc_list]
            density_matrix = DensityMatrix(np.mean([q_state.to_operator().to_matrix() for q_state in q_state_list],
                                                   axis=0))
            self.density_matrix_history.append(density_matrix)

            if self.target_type == 'state':
                self.state_fidelity_history.append(state_fidelity(self.target["dm"], density_matrix))
            else:  # Gate calibration task
                q_process_list = [Operator(qc) for qc in qc_list]

                prc_fidelity = np.mean([process_fidelity(q_process, Operator(self.target["gate"]))
                                        for q_process in q_process_list])
                avg_fidelity = np.mean([average_gate_fidelity(q_process, Operator(self.target["gate"]))
                                        for q_process in q_process_list])
                self.built_unitaries.append(q_process_list)
                self.process_fidelity_history.append(prc_fidelity)  # Avg process fidelity over the action batch
                self.avg_fidelity_history.append(avg_fidelity)  # Avg gate fidelity over the action batch
                # for i, input_state in enumerate(self.target["input_states"]):
                #     output_states = [DensityMatrix(Operator(qc) @ input_state["dm"] @ Operator(qc).adjoint())
                #                      for qc in qc_list]
                #     self.input_output_state_fidelity_history[i].append(
                #         np.mean([state_fidelity(input_state["target_state"]["dm"],
                #                                 output_state) for output_state in output_states]))
        elif self.abstraction_level == 'pulse':
            # Pulse simulation
            schedule_list = [schedule(qc, backend=self.backend, dt=self.backend.target.dt) for qc in qc_list]
            unitaries = self._simulate_pulse_schedules(schedule_list)
            # TODO: Line below yields an error if simulation is not done over a set of qubit (fails if third level of
            # TODO: transmon is simulated), adapt the target gate operator accordingly.
            unitaries = [Operator(np.array(unitary.y[0])) for unitary in unitaries]

            if self.target_type == 'state':
                density_matrix = DensityMatrix(np.mean([Statevector.from_int(0, dims=self._d).evolve(unitary)
                                                        for unitary in unitaries]))
                self.state_fidelity_history.append(state_fidelity(self.target["dm"], density_matrix))
            else:
                self.process_fidelity_history.append(np.mean([process_fidelity(unitary, self.target["gate"])
                                                              for unitary in unitaries]))
                self.avg_fidelity_history.append(np.mean([average_gate_fidelity(unitary, self.target["gate"])
                                                          for unitary in unitaries]))
            self.built_unitaries.append(unitaries)

    def gate_fidelity_from_process_tomography(self, qc_list: List[QuantumCircuit]):
        """
        Extract average gate and process fidelities from batch of Quantum Circuit for target gate
        """
        # Process tomography
        assert self.target_type == 'gate', "Target must be of type gate"
        batch_size = len(qc_list)

        exps = BatchExperiment([ProcessTomography(qc, backend=self.backend, physical_qubits=self.tgt_register)
                                for qc in qc_list])

        results = exps.run().block_for_results()
        process_results = [results.child_data(i).analysis_results(0) for i in range(batch_size)]
        Choi_matrices = [matrix.value for matrix in process_results]
        avg_gate_fidelity = np.mean([average_gate_fidelity(Choi_matrix, Operator(self.target["gate"]))
                                     for Choi_matrix in Choi_matrices])
        prc_fidelity = np.mean([process_fidelity(Choi_matrix, Operator(self.target["gate"]))
                                for Choi_matrix in Choi_matrices])

        self.process_fidelity_history.append(prc_fidelity)
        self.avg_fidelity_history.append(avg_gate_fidelity)
        return avg_gate_fidelity, prc_fidelity

    def _simulate_pulse_schedules(self, schedule_list: List[Union[pulse.Schedule, pulse.ScheduleBlock]]):
        """
        Method used to simulate pulse schedules, jit compatible
        """
        time_f = self.backend.target.dt * schedule_list[0].duration
        unitaries = self.solver.solve(
            t_span=Array([0.0, time_f]),
            y0=self.y_0,
            t_eval=[time_f],
            signals=schedule_list,
            method="jax_odeint",
        )

        return unitaries

    def clear_history(self):
        self.qc_history.clear()
        self.action_history.clear()
        self.reward_history.clear()
        if self.target_type == 'gate':
            self.avg_fidelity_history.clear()
            self.process_fidelity_history.clear()
            self.built_unitaries.clear()

        else:
            self.state_fidelity_history.clear()
            self.density_matrix_history.clear()

    def close(self) -> None:
        if isinstance(self.estimator, Runtime_Estimator):
            self.estimator.session.close()

    def __repr__(self):
        string = f"QuantumEnvironment composed of {self._n_qubits} qubits, \n"
        string += f"Defined target: {self.target_type} " \
                  f"({self.target.get('gate', None) if not None else self.target['dm']})\n"
        string += f"Physical qubits: {self.target['register']}\n"
        string += f"Backend: {self.backend},\n"
        string += f"Abstraction level: {self.abstraction_level},\n"
        string += f"Run options: N_shots ({self.n_shots}), Sampling_Pauli_space ({self.sampling_Pauli_space}), \n"
        return string

    @property
    def n_qubits(self):
        return self._n_qubits

    @n_qubits.setter
    def n_qubits(self, n_qubits):
        assert isinstance(n_qubits, int) and n_qubits > 0, "n_qubits must be a positive integer"
        self._n_qubits = n_qubits

    @property
    def layout(self):
        return self._layout

    @layout.setter
    def layout(self, layout: Layout):
        self._layout = layout

    @property
    def config_type(self):
        return self._config_type

    @property
    def config(self):
        return self._config

    @property
    def estimator(self) -> Estimator_type:
        return self._estimator

    @estimator.setter
    def estimator(self, estimator: BaseEstimator):
        self._estimator = estimator

    @property
    def step_tracker(self):
        return self._step_tracker
    @step_tracker.setter
    def step_tracker(self, step:int):
        assert step >= 0, 'step must be positive integer'
        self._step_tracker = step

    def to_json(self):
        return json.dumps({"n_qubits": self.n_qubits, "config": self._config,
                           "abstraction_level": self.abstraction_level,
                           "sampling_Pauli_space": self.sampling_Pauli_space,
                           "n_shots": self.n_shots,
                           "target_type": self.target_type,
                           "target": self.target,
                           "c_factor": self.c_factor,
                           "reward_history": self.reward_history,
                           "action_history": self.action_history,
                           "fidelity_history": self.avg_fidelity_history if self.target_type == "gate"
                           else self.state_fidelity_history,

                           })

    @classmethod
    def from_json(cls, json_str):
        """Return a MyCustomClass instance based on the input JSON string."""

        class_info = json.loads(json_str)
        abstraction_level = class_info["abstraction_level"]
        target = class_info["target"]
        n_shots = class_info["n_shots"]
        c_factor = class_info["c_factor"]
        sampling_Pauli_space = class_info["sampling_Pauli_space"]
        config = class_info["config"]
        q_env = cls(target, abstraction_level, config, None, sampling_Pauli_space,
                    n_shots, c_factor)
        q_env.reward_history = class_info["reward_history"]
        q_env.action_history = class_info["action_history"]
        if class_info["target_type"] == "gate":
            q_env.avg_fidelity_history = class_info["fidelity_history"]
        else:
            q_env.state_fidelity_history = class_info["fidelity_history"]
        return q_env
