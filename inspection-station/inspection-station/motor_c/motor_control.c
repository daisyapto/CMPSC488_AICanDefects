#include "motor_control.h"
#ifndef USE_HARDWARE
#define USE_HARDWARE 0
#endif
#if USE_HARDWARE
#include "DEV_Config.h"
#include "HR8825.h"
#endif

/* One worker controls both channels. Batches limit stop/reverse latency. */
#define BATCH_STEPS 4
#define MOUNT_FIXED_SPEED 60
#ifndef MOUNT_MAX_COMMAND_STEPS
#define MOUNT_MAX_COMMAND_STEPS 9000
#endif
#ifndef CONVEYOR_MAX_OSCILLATION_STEPS
#define CONVEYOR_MAX_OSCILLATION_STEPS 9000
#endif

static GMutex lock;
static GThread *worker = NULL;
static gboolean alive = FALSE, conveyor_on = FALSE, mount_on = FALSE;
static gboolean conveyor_oscillating = FALSE;
static gboolean conveyor_continuous = FALSE;
static int conveyor_dir = 1, mount_dir = 1;
static int conveyor_speed = 74; /* 70% of calibrated 53..83 range */
static int conveyor_steps_remaining = 0;
static int conveyor_steps_done = 0;
static int conveyor_steps_requested = 0;
static int mount_steps_remaining = 0;
static int mount_steps_requested = 0;
static int mount_position = 0;
static gboolean mount_referenced = FALSE;
static guint64 mount_command = 0;
static int oscillation_steps = 5000;
static int oscillation_leg_done = 0;
static unsigned long long oscillation_cycles_done = 0;
static guint64 conveyor_command = 0;

static gpointer worker_fn(gpointer ignored) {
    (void)ignored;
    while (TRUE) {
        gboolean c_on, m_on, c_osc;
        int c_dir, m_dir, c_speed, c_batch, m_batch;
        guint64 command, m_command;
        g_mutex_lock(&lock);
        if (!alive) { g_mutex_unlock(&lock); break; }
        c_on = conveyor_on; m_on = mount_on;
        c_dir = conveyor_dir; m_dir = mount_dir;
        c_speed = conveyor_speed;
        c_osc = conveyor_oscillating;
        command = conveyor_command;
        m_command = mount_command;
        c_batch = (c_on && c_osc) ? MIN(BATCH_STEPS, oscillation_steps - oscillation_leg_done) :
                  (c_on && conveyor_continuous) ? BATCH_STEPS :
                  (c_on ? MIN(BATCH_STEPS, conveyor_steps_remaining) : 0);
        m_batch = m_on ? MIN(mount_steps_remaining, BATCH_STEPS) : 0;
        g_mutex_unlock(&lock);
#if USE_HARDWARE
        if (c_batch > 0) {
            HR8825_SelectMotor(MOTOR1);
            HR8825_TurnStep(c_dir > 0 ? FORWARD : BACKWARD,
                            c_batch, 1100 - c_speed * 10);
            HR8825_Stop();
        }
        if (m_batch > 0) {
            HR8825_SelectMotor(MOTOR2);
            HR8825_TurnStep(m_dir > 0 ? FORWARD : BACKWARD,
                            m_batch, 1100 - MOUNT_FIXED_SPEED * 10);
            HR8825_Stop();
        }
#else
        if (c_batch > 0 || m_batch > 0) g_usleep(2500);
#endif
        if (c_batch > 0 && !c_osc) {
            g_mutex_lock(&lock);
            if (conveyor_on && !conveyor_oscillating && !conveyor_continuous &&
                conveyor_command == command && conveyor_dir == c_dir) {
                conveyor_steps_remaining = MAX(0, conveyor_steps_remaining - c_batch);
                conveyor_steps_done += c_batch;
                if (conveyor_steps_remaining == 0) conveyor_on = FALSE;
            }
            g_mutex_unlock(&lock);
        }
        if (c_batch > 0 && c_osc) {
            g_mutex_lock(&lock);
            /* Don't credit a batch against a new or stopped command. */
            if (conveyor_on && conveyor_oscillating &&
                conveyor_command == command && conveyor_dir == c_dir) {
                oscillation_leg_done += c_batch;
                if (oscillation_leg_done >= oscillation_steps) {
                    oscillation_leg_done = 0;
                    if (conveyor_dir < 0) oscillation_cycles_done++;
                    conveyor_dir = -conveyor_dir;
                }
            }
            g_mutex_unlock(&lock);
        }
        if (m_batch > 0) {
            g_mutex_lock(&lock);
            if (mount_command == m_command) {
                mount_position += m_dir * m_batch;
            }
            if (mount_on && mount_command == m_command && mount_dir == m_dir) {
                mount_steps_remaining = MAX(0, mount_steps_remaining - m_batch);
                if (mount_steps_remaining == 0) mount_on = FALSE;
            }
            g_mutex_unlock(&lock);
        }
        if (c_batch == 0 && m_batch == 0) g_usleep(20000);
    }
#if USE_HARDWARE
    HR8825_SelectMotor(MOTOR1); HR8825_Stop();
    HR8825_SelectMotor(MOTOR2); HR8825_Stop();
#endif
    return NULL;
}

gboolean motor_init(void) {
    g_mutex_init(&lock);
#if USE_HARDWARE
    if (DEV_ModuleInit()) { g_mutex_clear(&lock); return FALSE; }
    HR8825_SelectMotor(MOTOR1); HR8825_SetMicroStep(HARDWARD, "fullstep");
    HR8825_SelectMotor(MOTOR2); HR8825_SetMicroStep(HARDWARD, "fullstep");
    HR8825_SelectMotor(MOTOR1); HR8825_Stop();
    HR8825_SelectMotor(MOTOR2); HR8825_Stop();
#endif
    alive = TRUE;
    worker = g_thread_new("stepper-worker", worker_fn, NULL);
    return TRUE;
}
void motor_conveyor_start(int dir, int speed, int steps) {
    g_mutex_lock(&lock);
    conveyor_command++;
    conveyor_oscillating = FALSE;
    conveyor_continuous = FALSE;
    conveyor_steps_requested = CLAMP(steps, 1, CONVEYOR_MAX_OSCILLATION_STEPS);
    conveyor_steps_remaining = conveyor_steps_requested;
    conveyor_steps_done = 0;
    conveyor_dir = dir < 0 ? -1 : 1;
    conveyor_speed = CLAMP(speed, 53, 83);
    conveyor_on = TRUE;
    g_mutex_unlock(&lock);
}
void motor_conveyor_run_forever(int dir, int speed) {
    g_mutex_lock(&lock);
    conveyor_command++;
    conveyor_dir = dir < 0 ? -1 : 1;
    conveyor_speed = CLAMP(speed, 53, 83);
    conveyor_oscillating = FALSE;
    conveyor_continuous = TRUE;
    conveyor_steps_requested = 0;
    conveyor_steps_remaining = 0;
    conveyor_steps_done = 0;
    conveyor_on = TRUE;
    g_mutex_unlock(&lock);
}
void motor_conveyor_back_forth(int steps, int speed) {
    g_mutex_lock(&lock);
    conveyor_command++;
    conveyor_steps_remaining = 0;
    conveyor_continuous = FALSE;
    conveyor_steps_done = 0;
    conveyor_steps_requested = 0;
    oscillation_steps = CLAMP(steps, 1, CONVEYOR_MAX_OSCILLATION_STEPS);
    oscillation_leg_done = 0;
    oscillation_cycles_done = 0;
    conveyor_speed = CLAMP(speed, 53, 83);
    conveyor_dir = 1;
    conveyor_oscillating = TRUE;
    conveyor_on = TRUE;
    g_mutex_unlock(&lock);
}
void motor_conveyor_stop(void) {
    g_mutex_lock(&lock);
    conveyor_command++;
    conveyor_on = FALSE;
    conveyor_oscillating = FALSE;
    conveyor_continuous = FALSE;
    conveyor_steps_remaining = 0;
    g_mutex_unlock(&lock);
}
void motor_conveyor_settings(int dir, int speed) {
    g_mutex_lock(&lock);
    /* Speed is adjustable during oscillation; direction is automatic. */
    if (!conveyor_oscillating && !conveyor_continuous) conveyor_dir = dir < 0 ? -1 : 1;
    conveyor_speed = CLAMP(speed, 53, 83);
    g_mutex_unlock(&lock);
}
void motor_mount_move(int dir, int steps) {
    g_mutex_lock(&lock);
    mount_command++;
    mount_dir = dir < 0 ? -1 : 1;
    mount_steps_remaining = CLAMP(steps, 1, MOUNT_MAX_COMMAND_STEPS);
    mount_steps_requested = mount_steps_remaining;
    mount_on = TRUE;
    g_mutex_unlock(&lock);
}
/* Establish the physical 12oz/bottom position as zero. Stop the mount first. */
void motor_mount_set_reference(void) {
    g_mutex_lock(&lock);
    if (!mount_on) {
        mount_position = 0;
        mount_referenced = TRUE;
    }
    g_mutex_unlock(&lock);
}

/* Absolute moves from the calibrated reference: 12oz=0, 16oz=3300, 19.2oz=6600. */
gboolean motor_mount_goto_position(int position) {
    gboolean ok = FALSE;
    g_mutex_lock(&lock);
    if (mount_referenced && position >= 0 && position <= 6600) {
        int delta = position - mount_position;
        mount_command++;
        mount_dir = delta < 0 ? -1 : 1;
        mount_steps_requested = ABS(delta);
        mount_steps_remaining = ABS(delta);
        mount_on = (delta != 0);
        ok = TRUE;
    }
    g_mutex_unlock(&lock);
    return ok;
}

void motor_mount_stop(void) {
    g_mutex_lock(&lock);
    mount_command++;
    mount_on = FALSE;
    mount_steps_remaining = 0;
    g_mutex_unlock(&lock);
}
void motor_stop_all(void) {
    g_mutex_lock(&lock);
    mount_command++;
    conveyor_command++;
    conveyor_on = FALSE;
    conveyor_oscillating = FALSE;
    conveyor_continuous = FALSE;
    conveyor_steps_remaining = 0;
    mount_on = FALSE;
    mount_steps_remaining = 0;
    g_mutex_unlock(&lock);
}
MotorStatus motor_status(void) {
    MotorStatus s;
    g_mutex_lock(&lock);
    s.conveyor_running = conveyor_on;
    s.conveyor_oscillating = conveyor_oscillating;
    s.conveyor_continuous = conveyor_continuous;
    s.mount_running = mount_on;
    s.conveyor_direction = conveyor_dir;
    s.mount_direction = mount_dir;
    s.conveyor_speed = conveyor_speed;
    s.conveyor_steps_requested = conveyor_steps_requested;
    s.conveyor_steps_done = conveyor_steps_done;
    s.mount_steps_remaining = mount_steps_remaining;
    s.mount_steps_requested = mount_steps_requested;
    s.mount_position = mount_referenced ? mount_position : -999999;
    s.conveyor_oscillation_steps = oscillation_steps;
    s.conveyor_leg_steps_done = oscillation_leg_done;
    s.conveyor_cycles_completed = oscillation_cycles_done;
    g_mutex_unlock(&lock);
    s.hardware = USE_HARDWARE;
    return s;
}
void motor_shutdown(void) {
    motor_stop_all();
    g_mutex_lock(&lock); alive = FALSE; g_mutex_unlock(&lock);
    if (worker) { g_thread_join(worker); worker = NULL; }
#if USE_HARDWARE
    DEV_ModuleExit();
#endif
    g_mutex_clear(&lock);
}
