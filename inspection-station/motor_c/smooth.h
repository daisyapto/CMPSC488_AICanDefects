#ifndef SMOOTH_H
#define SMOOTH_H
/* Precise step timing for the conveyor - see smooth.c */
#include "motor_control.h"
#ifndef USE_HARDWARE
#define USE_HARDWARE 0
#endif
int smooth_init(void);
void smooth_move(int dir, int speed, int steps);
void smooth_auto(int speed, int steps);
void smooth_run(int dir, int speed);
void smooth_speed(int speed);
void smooth_stop(void);
void smooth_stop_now(void);
void smooth_status(MotorStatus *s);
void smooth_shutdown(void);
#endif
