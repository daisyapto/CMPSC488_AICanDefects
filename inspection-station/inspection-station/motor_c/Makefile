# motord: the team's motor code (motor_control.c, HR8825.c, DEV_Config.c - unchanged)
# with a command interface for the inspection station. No GTK needed.
#
#   sudo apt install build-essential libglib2.0-dev liblgpio-dev
#   make            -> ./motord        (hardware)
#   make sim        -> ./motord_sim    (no GPIO, for testing)
#
# MAX_STEPS raises the per-command step limit (motor program: 9000).
MAX_STEPS ?= 50000

CC := gcc
GLIB := $(shell pkg-config --cflags --libs glib-2.0)
CFLAGS := -std=gnu11 -O2 -Wall -I. -DCONVEYOR_MAX_OSCILLATION_STEPS=$(MAX_STEPS)

.PHONY: all sim clean
all: motord

motord: motord.c motor_control.c HR8825.c DEV_Config.c *.h Makefile
	$(CC) $(CFLAGS) -DUSE_HARDWARE=1 motord.c motor_control.c HR8825.c DEV_Config.c -o $@ $(GLIB) -llgpio -lm

sim: motord_sim
motord_sim: motord.c motor_control.c motor_control.h Makefile
	$(CC) $(CFLAGS) -DUSE_HARDWARE=0 motord.c motor_control.c -o $@ $(GLIB) -lm

clean:
	rm -f motord motord_sim
