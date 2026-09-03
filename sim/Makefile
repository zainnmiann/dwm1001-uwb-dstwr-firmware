# Host simulator. Links the firmware's portable modules unmodified.
CC      ?= cc
SRC     := ../src
VENDOR  := ../vendor/deca_driver
CFLAGS  ?= -std=c11 -O1 -g -Wall -Wextra -I. -Istub -I$(SRC) -I$(VENDOR)
# Header dependencies. Without these a stale object silently survives an
# edit to sim.h -- which cost an afternoon: sim_radio.o kept an older
# nclk_t layout and read drift_ppm where phase_dtu now lives.
CFLAGS  += -MMD -MP

OBJS := sim_main.o sim_radio.o sim_trace.o cfg.o timing.o slots.o ranging.o sync.o

all: run

%.o: %.c
	$(CC) $(CFLAGS) -c -o $@ $<
%.o: $(SRC)/%.c
	$(CC) $(CFLAGS) -c -o $@ $<

sim: $(OBJS)
	$(CC) -o $@ $(OBJS) -lm

run: sim
	./sim

clean:
	rm -f sim *.o *.d

-include $(OBJS:.o=.d)

.PHONY: all run clean
