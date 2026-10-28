/*
 * smooth.c - precise step timing for the conveyor (Motor 1 on the Waveshare HAT).
 *
 * Same pins (DEV_Config.h), same speed scale (53..83 -> half period 1100 - 10*speed us),
 * same commands as motor_control.c - but the pulses are generated differently, so a
 * busy Pi can't make the motor run rough or lose steps:
 *   - every edge is scheduled against an absolute clock and placed with a short
 *     busy-wait (no sleep that can wake late); a late edge never causes a short
 *     pulse or a catch-up burst - the schedule continues from when it happened
 *   - acceleration / deceleration ramp, so the motor never jumps to full speed
 *   - the driver stays enabled for the whole move (not switched off every 4 steps)
 * Runs inside motord, which pins itself to its own CPU core with real-time priority.
 * Based on the conveyor process in conveyor_inspection.py.
 */
#define _GNU_SOURCE
#include "smooth.h"
#include <pthread.h>
#include <stdatomic.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#if USE_HARDWARE
#include <lgpio.h>
#include "DEV_Config.h"
static int h = -1;
static void wr(int pin, int v) { lgGpioWrite(h, pin, v); }
#else
#define M1_ENABLE_PIN 12
#define M1_DIR_PIN 13
#define M1_STEP_PIN 19
static void wr(int pin, int v) { (void)pin; (void)v; }
#endif

#define ACCEL 4000.0              /* steps/s^2 */
#define RAMP_START 450.0          /* steps/s the ramp starts and ends at */
#define AUTO_DWELL_NS 300000000LL /* pause at each end in Auto */

static pthread_t thr;
static pthread_mutex_t mx = PTHREAD_MUTEX_INITIALIZER;
static atomic_int alive, seq;

/* shared state (mutex) - commands set fields + a flag, the worker picks them up */
static int c_on, c_dir = 1, c_speed = 74, c_mode;   /* mode 0 move, 1 auto, 2 continuous */
static int c_req, c_done, c_cycles, c_auto_steps = 5000;
static int f_start, f_stop_soft, f_stop_hard;

static long long now_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (long long)t.tv_sec * 1000000000LL + t.tv_nsec;
}
static void wait_until(long long t) {
    /* Sleep most of the gap, busy-wait only the last ~80 us. Sleeping part of every
       pulse also keeps the thread under Linux's real-time limit (a real-time thread
       that never sleeps is paused for ~50 ms every second - a hiccup in the motor). */
    long long gap = t - now_ns();
    if (gap > 150000) {
        struct timespec s = { 0, gap - 80000 };
        nanosleep(&s, NULL);
    }
    while (now_ns() < t) { }
}
static double rate_of(int speed) { return 1e6 / (2.0 * (1100 - speed * 10)); }

static void *worker(void *arg) {
    (void)arg;
    int dir = 0, mode = 0, req = 0, done = 0, speed = 74, seen = -1, auto_steps = 5000;
    double rate = 0, start = RAMP_START, target = rate_of(74);
    long long next = 0, resume_at = 0;

    while (atomic_load(&alive)) {
        int s = atomic_load(&seq);
        if (s != seen) {                        /* a command arrived */
            seen = s;
            pthread_mutex_lock(&mx);
            if (f_stop_hard) {                   /* Stop all / E-stop: at once */
                f_stop_hard = 0; f_start = 0; f_stop_soft = 0;
                dir = 0; mode = 0; resume_at = 0; c_on = 0;
                wr(M1_STEP_PIN, 0); wr(M1_ENABLE_PIN, 0);
            }
            if (f_start) {                       /* new move / auto / run */
                f_start = 0;
                dir = c_dir; mode = c_mode; req = c_req; done = 0; speed = c_speed;
                auto_steps = c_auto_steps; resume_at = 0;
                target = rate_of(speed);
                start = target < RAMP_START ? target : RAMP_START;
                rate = start;
                wr(M1_DIR_PIN, dir > 0 ? 0 : 1);        /* FORWARD = DIR low, as HR8825.c */
                wr(M1_ENABLE_PIN, 1);
                next = now_ns() + 2000000;               /* 2 ms driver wake-up / DIR set-up */
            }
            if (f_stop_soft) {                   /* Stop: ramp down, then driver off */
                f_stop_soft = 0;
                if (dir) {
                    int stop_steps = (int)((rate * rate - start * start) / (2 * ACCEL)) + 1;
                    mode = 0; c_mode = 0; req = done + stop_steps; c_req = req;
                } else {
                    mode = 0; resume_at = 0; c_on = 0;
                    wr(M1_ENABLE_PIN, 0);
                }
            }
            if (c_speed != speed) { speed = c_speed; target = rate_of(speed); }
            auto_steps = c_auto_steps;
            pthread_mutex_unlock(&mx);
        }

        if (!dir) {
            if (mode == 1 && resume_at && now_ns() >= resume_at) {   /* Auto: next pass */
                pthread_mutex_lock(&mx);
                dir = c_dir = -c_dir; req = c_req = auto_steps; done = 0; c_done = 0;
                pthread_mutex_unlock(&mx);
                rate = start;
                wr(M1_DIR_PIN, dir > 0 ? 0 : 1);
                next = now_ns() + 2000000;
                resume_at = 0;
            } else {
                struct timespec z = { 0, 2000000 };
                nanosleep(&z, NULL);
            }
            continue;
        }

        /* trapezoidal speed profile */
        double dv = ACCEL / rate;
        int remaining = req ? req - done : 1 << 30;
        double stopping = (rate * rate - start * start) / (2 * ACCEL);
        if (remaining <= stopping) rate = rate - dv > start ? rate - dv : start;
        else if (rate < target) rate = rate + dv < target ? rate + dv : target;
        else if (rate > target) rate = rate - dv > target ? rate - dv : target;
        long long half = (long long)(5e8 / rate);

        /* one step pulse - a late edge re-bases the schedule (no short pulse, no burst) */
        wait_until(next);
        wr(M1_STEP_PIN, 1);
        long long t = now_ns();
        next = (next > t ? next : t) + half;
        wait_until(next);
        wr(M1_STEP_PIN, 0);
        t = now_ns();
        next = (next > t ? next : t) + half;
        done++;

        pthread_mutex_lock(&mx);
        c_done = done;
        if (req && done >= req) {
            if (mode == 1) {                     /* Auto: hold, then reverse */
                if (dir < 0) c_cycles++;
                resume_at = now_ns() + AUTO_DWELL_NS;
                dir = 0;
            } else {
                dir = 0; c_on = 0;
                wr(M1_ENABLE_PIN, 0);
            }
        }
        pthread_mutex_unlock(&mx);
    }
    wr(M1_STEP_PIN, 0);
    wr(M1_ENABLE_PIN, 0);
    return NULL;
}

int smooth_init(void) {
#if USE_HARDWARE
    h = lgGpiochipOpen(0);
    if (h < 0) return 0;
    const int pins[] = { M1_ENABLE_PIN, M1_DIR_PIN, M1_STEP_PIN, M1_M0_PIN, M1_M1_PIN, M1_M2_PIN,
                         M2_ENABLE_PIN, M2_DIR_PIN, M2_STEP_PIN, M2_M0_PIN, M2_M1_PIN, M2_M2_PIN };
    for (unsigned i = 0; i < sizeof pins / sizeof pins[0]; i++)
        if (lgGpioClaimOutput(h, 0, pins[i], 0) < 0) return 0;
#endif
    atomic_store(&alive, 1);
    return pthread_create(&thr, NULL, worker, NULL) == 0;   /* inherits core + priority */
}

static void kick(void) { atomic_fetch_add(&seq, 1); }

static void begin(int dir, int speed, int steps, int mode) {
    pthread_mutex_lock(&mx);
    c_dir = dir < 0 ? -1 : 1; c_speed = speed; c_req = steps; c_done = 0; c_mode = mode;
    if (mode == 1) { c_auto_steps = steps; c_cycles = 0; }
    c_on = 1; f_start = 1; f_stop_soft = 0;
    pthread_mutex_unlock(&mx);
    kick();
}
void smooth_move(int dir, int speed, int steps) { begin(dir, speed, steps, 0); }
void smooth_auto(int speed, int steps) { begin(1, speed, steps, 1); }
void smooth_run(int dir, int speed) { begin(dir, speed, 0, 2); }
void smooth_speed(int speed) {
    pthread_mutex_lock(&mx); c_speed = speed; pthread_mutex_unlock(&mx); kick();
}
void smooth_stop(void) {
    pthread_mutex_lock(&mx); f_stop_soft = 1; pthread_mutex_unlock(&mx); kick();
}
void smooth_stop_now(void) {
    pthread_mutex_lock(&mx); f_stop_hard = 1; f_start = 0; c_on = 0; pthread_mutex_unlock(&mx); kick();
}
void smooth_status(MotorStatus *s) {
    memset(s, 0, sizeof *s);
    pthread_mutex_lock(&mx);
    s->conveyor_running = c_on;
    s->conveyor_oscillating = c_on && c_mode == 1;
    s->conveyor_continuous = c_on && c_mode == 2;
    s->conveyor_direction = c_dir;
    s->conveyor_speed = c_speed;
    s->conveyor_steps_done = c_done;
    s->conveyor_steps_requested = c_req;
    s->conveyor_leg_steps_done = c_done;
    s->conveyor_oscillation_steps = c_auto_steps;
    s->conveyor_cycles_completed = c_cycles;
    s->mount_direction = 1;
    s->mount_position = -999999;
    s->hardware = USE_HARDWARE;
    pthread_mutex_unlock(&mx);
}
void smooth_shutdown(void) {
    smooth_stop_now();
    atomic_store(&alive, 0);
    pthread_join(thr, NULL);
#if USE_HARDWARE
    lgGpiochipClose(h);
#endif
}
