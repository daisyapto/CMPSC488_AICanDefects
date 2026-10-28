#ifndef MOTOR_CONTROL_H
#define MOTOR_CONTROL_H
#include <glib.h>
typedef struct {
    gboolean conveyor_running, mount_running, hardware;
    gboolean conveyor_oscillating, conveyor_continuous;
    int conveyor_direction, mount_direction, conveyor_speed, mount_steps_remaining, mount_steps_requested;
    int conveyor_oscillation_steps, conveyor_leg_steps_done;
    int conveyor_steps_requested, conveyor_steps_done;
    int mount_position;
    unsigned long long conveyor_cycles_completed;
} MotorStatus;
gboolean motor_init(void);
void motor_conveyor_start(int dir, int speed, int steps);
void motor_conveyor_back_forth(int steps, int speed);
void motor_conveyor_run_forever(int dir, int speed);
void motor_conveyor_stop(void);
void motor_conveyor_settings(int dir, int speed);
void motor_mount_move(int dir, int steps);
void motor_mount_set_reference(void);
gboolean motor_mount_goto_position(int position);
void motor_mount_stop(void);
void motor_stop_all(void);
MotorStatus motor_status(void);
void motor_shutdown(void);
#endif
