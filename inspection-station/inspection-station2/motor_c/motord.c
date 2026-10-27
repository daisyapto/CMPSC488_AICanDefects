/*
 * motord - the team's motor program (motor_control.c + HR8825.c + DEV_Config.c,
 * unchanged) with a text command interface instead of the GTK window, so the
 * inspection station dashboard drives the Waveshare HAT exactly like ./motor.
 *
 * One command per line on stdin, one reply line on stdout:
 *   move DIR SPEED STEPS     motor_conveyor_start      (Forward / Reverse buttons)
 *   auto SPEED STEPS         motor_conveyor_back_forth (Auto button)
 *   run DIR SPEED            motor_conveyor_run_forever
 *   speed DIR SPEED          motor_conveyor_settings   (speed slider)
 *   stop                     motor_conveyor_stop
 *   mount DIR STEPS          motor_mount_move          (Camera up / down)
 *   goto POSITION            motor_mount_goto_position (12 / 16 / 19.2 oz presets)
 *   ref                      motor_mount_set_reference (Set 12oz reference)
 *   mstop                    motor_mount_stop
 *   stopall                  motor_stop_all
 *   status                   S <MotorStatus fields>
 *   quit
 * DIR = 1 / -1, SPEED = 53..83 (main.c: 53 + slider% * 30 / 100).
 * When stdin closes (the station stops or crashes) all motors stop.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "motor_control.h"

#ifndef USE_HARDWARE
#define USE_HARDWARE 0
#endif

int main(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (!motor_init()) {
        printf("ERR motor initialization failed (GPIO busy? is ./motor running?)\n");
        return 1;
    }
    printf("OK ready hardware=%d max_steps=%d\n", USE_HARDWARE, CONVEYOR_MAX_OSCILLATION_STEPS);

    char line[256];
    while (fgets(line, sizeof line, stdin)) {
        char cmd[32] = "";
        int a = 0, b = 0, c = 0;
        int n = sscanf(line, "%31s %d %d %d", cmd, &a, &b, &c);
        if (n < 1) continue;
        if (!strcmp(cmd, "move") && n == 4)        motor_conveyor_start(a, b, c);
        else if (!strcmp(cmd, "auto") && n == 3)   motor_conveyor_back_forth(b, a);
        else if (!strcmp(cmd, "run") && n == 3)    motor_conveyor_run_forever(a, b);
        else if (!strcmp(cmd, "speed") && n == 3)  motor_conveyor_settings(a, b);
        else if (!strcmp(cmd, "stop"))             motor_conveyor_stop();
        else if (!strcmp(cmd, "mount") && n == 3)  motor_mount_move(a, b);
        else if (!strcmp(cmd, "goto") && n == 2) {
            if (!motor_mount_goto_position(a)) { printf("ERR set the 12oz reference first\n"); continue; }
        }
        else if (!strcmp(cmd, "ref"))              motor_mount_set_reference();
        else if (!strcmp(cmd, "mstop"))            motor_mount_stop();
        else if (!strcmp(cmd, "stopall"))          motor_stop_all();
        else if (!strcmp(cmd, "status")) {
            MotorStatus s = motor_status();
            printf("S %d %d %d %d %d %d %d %d %d %d %d %d %d %d %llu %d\n",
                   s.conveyor_running, s.conveyor_oscillating, s.conveyor_continuous,
                   s.conveyor_direction, s.conveyor_speed,
                   s.conveyor_steps_done, s.conveyor_steps_requested,
                   s.conveyor_leg_steps_done, s.conveyor_oscillation_steps,
                   s.mount_running, s.mount_direction,
                   s.mount_steps_requested - s.mount_steps_remaining, s.mount_steps_requested,
                   s.mount_position, s.conveyor_cycles_completed, s.hardware);
            continue;
        }
        else if (!strcmp(cmd, "quit"))             break;
        else { printf("ERR unknown command: %s", line); continue; }
        printf("OK\n");
    }
    motor_shutdown();
    return 0;
}
