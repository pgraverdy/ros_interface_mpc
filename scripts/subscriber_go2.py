#!/usr/bin/env python3

# Copyright 2016 Open Source Robotics Foundation, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import rclpy
from rclpy.node import Node
import numpy as np

import pinocchio as pin
from ros_interface_mpc.msg import Torque, RobotState
from sensor_msgs.msg import JointState
from tf2_ros import TransformBroadcaster, TransformStamped
from geometry_msgs.msg import Quaternion
from rclpy.qos import QoSProfile
from simple_mpc import IDSolver

from simulation_args import SimulationArgs
from simulation_utils import (
    addFloor,
    removeBVHModelsIfAny,
    setPhysicsProperties,
    Simulation,
    addSystemCollisionPairs
)

from robot_utils import loadGo2, loadHandlerGo2
from proxsuite_nlp import manifolds

class SimulationWrapper():

    def __init__(self):
        # Load the robot model 
        self.rmodel, geom_model = loadGo2()
        v0 = np.zeros(self.rmodel.nv)
        args = SimulationArgs()
        np.random.seed(args.seed)
        pin.seed(args.seed)
        q0 = np.array([0, 0, 0.335, 0, 0, 0, 1,
            0.068, 0.785, -1.440,
            -0.068, 0.785, -1.440,
            0.068, 0.785, -1.440,
            -0.068, 0.785, -1.440,
        ])
        # Add plane in geom_model
        visual_model = geom_model.copy()
        addFloor(geom_model, visual_model)

        # Set simulation properties
        setPhysicsProperties(geom_model, args.material, args.compliance)
        removeBVHModelsIfAny(geom_model)
        addSystemCollisionPairs(self.rmodel, geom_model, q0)

        # Remove all pair of collision which does not concern floor collision
        i = 0
        while i < len(geom_model.collisionPairs):
            cp = geom_model.collisionPairs[i]
            if geom_model.geometryObjects[cp.first].name != 'floor' and geom_model.geometryObjects[cp.second].name != 'floor':
                geom_model.removeCollisionPair(cp)
            else:
                i = i + 1
        
        # Create the simulator object
        self.simulator = Simulation(self.rmodel, geom_model, visual_model, q0, v0, args) 



class MpcSubscriber(Node):

    def __init__(self):
        # Initialization of node
        super().__init__('mpc_subscriber')
        self.declare_parameter('mpc_type')
        self.parameter = self.get_parameter('mpc_type')
        self.start_mpc = False
        
        # Define state publisher
        qos_profile = QoSProfile(depth=10)
        self.joint_pub = self.create_publisher(JointState, 'joint_states', qos_profile)
        self.robot_pub = self.create_publisher(RobotState, 'robot_states', 10)
        self.broadcaster = TransformBroadcaster(self, qos=qos_profile)
        
        # Define command subscriber
        self.subscription = self.create_subscription(
            Torque,
            'command',
            self.listener_callback,
            qos_profile)
        self.subscription  # prevent unused variable warning

        # Define at which rate the simulation state is sent to rviz
        timer_period = 0.001  # seconds
        self.timer = self.create_timer(timer_period, self.timer_callback)

        # Message declarations for odometry
        self.odom_trans = TransformStamped()
        self.odom_trans.header.frame_id = 'world'
        self.odom_trans.child_frame_id = 'base'
        self.robot_state = RobotState()
        
        # Message declarations for torque
        self.wrapper = SimulationWrapper()
        self.ndx = self.wrapper.rmodel.nv * 2
        self.nq = self.wrapper.rmodel.nq
        self.nu = self.wrapper.rmodel.nv - 6
        self.torque_simu = np.zeros(self.wrapper.rmodel.nv)
        self.current_torque = np.zeros(self.wrapper.rmodel.nv - 6)
        self.space = manifolds.MultibodyPhaseSpace(self.wrapper.rmodel)

        # Message declaration for joint states
        self.measure = JointState()
        self.measure.name = ["FL_hip_joint",
            "FL_thigh_joint",
            "FL_calf_joint",
            "FR_hip_joint",
            "FR_thigh_joint",
            "FR_calf_joint",
            "RL_hip_joint",
            "RL_thigh_joint",
            "RL_calf_joint",
            "RR_hip_joint",
            "RR_thigh_joint",
            "RR_calf_joint"
        ]
        self.measure.position = [0., 0., 0., # FL
                                 0., 0., 0., # FR
                                 0., 0., 0., # HL
                                 0., 0., 0., # HR
        ]
        self.robot_state.position = self.measure.position
        self.measure.velocity = [0., 0., 0., # FL
                                 0., 0., 0., # FR
                                 0., 0., 0., # HL
                                 0., 0., 0., # HR
        ]
        self.robot_state.velocity = self.measure.velocity
        self.controlled_joint_ids = [0, 1, 2,
                                     3, 4, 5,
                                     6, 7, 8,
                                     9, 10, 11, 
        ]
        
        # Initial state of the robot with feedforward torque equal to
        # gravity-compensating torque in half-sitting
        q_current, v_current = self.wrapper.simulator.get_state()
    
        self.x0 = np.concatenate((q_current, v_current))
        self.u0 = np.array([-3.71, -1.81,  5.25,  
                            3.14, -1.37, 5.54, 
                            -1.39, -1.09,  3.36,  
                            1.95, -0.61,  3.61])
        self.K0 = np.zeros((self.nu, self.ndx))

        # Set state message using latest simulation measure
        self.set_messages(q_current, v_current)
        
        # Define default PD controller that runs before MPC launch
        gain = 100
        self.Kp = np.identity(self.nu) * gain
        self.Kd = np.identity(self.nu) * 1

        # Build whole-body control layer depending on the
        # type of MPC in use
        self.WB_solver = None

        if self.parameter.value == "kinodynamics":
            self.handler = loadHandlerGo2()
            
            contact_ids = self.handler.getFeetIds()
            id_conf = dict(
                contact_ids=contact_ids,
                x0=self.handler.getState(),
                mu=0.8,
                Lfoot=0.01,
                Wfoot=0.01,
                force_size=3,
                kd=0,
                w_force=100,
                w_acc=1,
                verbose=False,
            )

            self.WB_solver = IDSolver()
            self.WB_solver.initialize(id_conf, self.handler.getModel())
            

    def listener_callback(self, msg):
        #self.get_logger().info('I heard: "%s"' % msg.x0[0])
        
        if self.parameter.value == "fulldynamics":
            self.u0 = np.array(msg.u0.tolist())
            self.x0 = np.array(msg.x0.tolist())
            self.K0 = np.array(msg.riccati.tolist()).reshape((self.nu, self.ndx))
        elif self.parameter.value == "kinodynamics":
            self.contact_states = msg.contact_states
            self.forces = np.array(msg.forces)
            self.a0 = np.array(msg.a0)

        self.start_mpc = True
    
    def timer_callback(self):
        q_current, v_current = self.wrapper.simulator.get_state()
        
        if not(self.start_mpc):
            self.current_torque = self.u0 - self.Kp @ (q_current[7:] - self.x0[7:self.nq]) - self.Kd @ v_current[6:]
        else:
            if self.parameter.value == "fulldynamics":
                x_measured = np.concatenate((q_current, v_current))
                self.current_torque = self.u0 - self.K0 @ self.space.difference(x_measured, self.x0)
            elif self.parameter.value == "kinodynamics":
                self.handler.updateState(q_current, v_current, True)
                self.WB_solver.solve_qp(
                    self.handler.getData(),
                    self.contact_states,
                    v_current,
                    self.a0,
                    self.forces,
                    self.handler.getMassMatrix(),
                )
                self.current_torque = self.WB_solver.solved_torque
        self.torque_simu[6:] = self.current_torque
        self.wrapper.simulator.execute(self.torque_simu)

        self.set_messages(q_current, v_current)
        self.joint_pub.publish(self.measure)
        self.robot_pub.publish(self.robot_state)
        self.broadcaster.sendTransform(self.odom_trans)
        #self.get_logger().info('Publishing: "%s"' % q_current)
    
    def set_messages(self, q_current, v_current):
        self.measure.header.stamp = self.get_clock().now().to_msg()
        for meas_id, joint_id in enumerate(self.controlled_joint_ids):
            self.measure.position[joint_id] = q_current[7 + meas_id]
            self.measure.velocity[joint_id] = v_current[6 + meas_id]
            self.robot_state.position[joint_id] = q_current[7 + meas_id]
            self.robot_state.velocity[joint_id] = v_current[6 + meas_id]

        self.odom_trans.header.stamp = self.get_clock().now().to_msg()
        self.odom_trans.transform.translation.x = q_current[0]
        self.odom_trans.transform.translation.y = q_current[1]
        self.odom_trans.transform.translation.z = q_current[2]
        self.odom_trans.transform.rotation = Quaternion(x=q_current[3], 
                                                        y=q_current[4], 
                                                        z=q_current[5], 
                                                        w=q_current[6])
        self.robot_state.transform.translation.x = q_current[0]
        self.robot_state.transform.translation.y = q_current[1]
        self.robot_state.transform.translation.z = q_current[2]
        self.robot_state.transform.rotation = Quaternion(x=q_current[3], 
                                                         y=q_current[4], 
                                                         z=q_current[5], 
                                                         w=q_current[6])
        self.robot_state.twist.linear.x = v_current[0]
        self.robot_state.twist.linear.y = v_current[1]
        self.robot_state.twist.linear.z = v_current[2]
        self.robot_state.twist.angular.x = v_current[3]
        self.robot_state.twist.angular.y = v_current[4]
        self.robot_state.twist.angular.z = v_current[5]

def main(args=None):
    rclpy.init(args=args)

    mpc_subscriber = MpcSubscriber()

    rclpy.spin(mpc_subscriber)

    # Destroy the node explicitly
    # (optional - otherwise it will be done automatically
    # when the garbage collector destroys the node object)
    mpc_subscriber.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
