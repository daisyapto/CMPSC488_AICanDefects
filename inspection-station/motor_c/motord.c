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
#define _GNU_SOURCE
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <unistd.h>
#include "motor_control.h"
#include "smooth.h"

#ifndef USE_HARDWARE
#define USE_HARDWARE 0
#endif

/* Replies go to the original stdout; the Waveshare DEBUG() printf output
   ("Debug : motor 1 formward" ...) is moved to stderr so it can't mix with them. */
static FILE *reply;
#define printf(...) fprintf(reply, __VA_ARGS__)

int main(int argc, char **argv) {
    int smooth = argc > 1 && !strcmp(argv[1], "--smooth");   /* precise timing (smooth.c) */
    reply = fdopen(dup(STDOUT_FILENO), "w");
    setvbuf(reply, NULL, _IOLBF, 0);
    dup2(STDERR_FILENO, STDOUT_FILENO);

    /* The step timing is software sleeps (DEV_Delay_us). Next to the camera and the
       dashboard those sleeps wake late and the motor runs rough, so: own CPU core
       (the station keeps off it) and real-time priority. Set before motor_init so the
       stepper thread inherits both. */
    long ncpu = sysconf(_SC_NPROCESSORS_ONLN);
    int core = ncpu > 1 ? (int)ncpu - 1 : 0;
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(core, &set);
    int pinned = sched_setaffinity(0, sizeof set, &set) == 0;
    struct sched_param sp = { .sched_priority = 60 };
    int rt = sched_setscheduler(0, SCHED_FIFO, &sp) == 0;
    if (!rt) setpriority(PRIO_PROCESS, 0, -15);          /* works only with privilege */

    if (smooth ? !smooth_init() : !motor_init()) {
        printf("ERR motor initialization failed (GPIO busy? is ./motor running?)\n");
        return 1;
    }
    /* Only the stepper thread (created by the init above) keeps the motor core and
       real-time priority; this command thread goes back to the other cores at normal
       priority, so it can never take time from the step pulses (or be starved by them). */
    if (ncpu > 1) {
        cpu_set_t others;
        CPU_ZERO(&others);
        for (int i = 0; i < ncpu; i++) if (i != core) CPU_SET(i, &others);
        sched_setaffinity(0, sizeof others, &others);
    }
    struct sched_param normal = { .sched_priority = 0 };
    sched_setscheduler(0, SCHED_OTHER, &normal);

    printf("OK ready hardware=%d max_steps=%d core=%d pinned=%d realtime=%d timing=%s\n",
           USE_HARDWARE, CONVEYOR_MAX_OSCILLATION_STEPS, core, pinned, rt, smooth ? "precise" : "team");

    char line[256];
    while (fgets(line, sizeof line, stdin)) {
        char cmd[32] = "";
        int a = 0, b = 0, c = 0;
        int n = sscanf(line, "%31s %d %d %d", cmd, &a, &b, &c);
        if (n < 1) continue;
        if (smooth) {                                    /* precise timing: conveyor only */
            if (!strcmp(cmd, "move") && n == 4)        smooth_move(a, b, c);
            else if (!strcmp(cmd, "auto") && n == 3)   smooth_auto(a, b);
            else if (!strcmp(cmd, "run") && n == 3)    smooth_run(a, b);
            else if (!strcmp(cmd, "speed") && n == 3)  smooth_speed(b);
            else if (!strcmp(cmd, "stop"))             smooth_stop();
            else if (!strcmp(cmd, "stopall") || !strcmp(cmd, "mstop")) smooth_stop_now();
            else if (!strcmp(cmd, "status")) {
                MotorStatus s; smooth_status(&s);
                printf("S %d %d %d %d %d %d %d %d %d 0 1 0 0 -999999 %llu %d\n",
                       s.conveyor_running, s.conveyor_oscillating, s.conveyor_continuous,
                       s.conveyor_direction, s.conveyor_speed, s.conveyor_steps_done,
                       s.conveyor_steps_requested, s.conveyor_leg_steps_done,
                       s.conveyor_oscillation_steps, s.conveyor_cycles_completed, s.hardware);
                continue;
            }
            else if (!strcmp(cmd, "quit"))             break;
            else { printf("ERR not available with precise timing: %s", line); continue; }
            printf("OK\n");
            continue;
        }
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
    if (smooth) smooth_shutdown(); else motor_shutdown();
    return 0;
}
