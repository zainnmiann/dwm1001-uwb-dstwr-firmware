# Fork-per-node harness: the real role loops, off target.
CC      ?= cc
SRC     := ../../src
VENDOR  := ../../vendor/deca_driver
CFLAGS  ?= -std=c11 -O1 -g -Wall -Wextra
CFLAGS  += -I. -Istub -I../stub -I.. -I$(SRC) -I$(VENDOR)
# Header dependencies. Their absence in sim/Makefile once cost an afternoon to
# a stale object holding an older struct layout.
CFLAGS  += -MMD -MP

LOCAL := harness.o ipc.o node_radio.o node_uart.o node_stubs.o
FW    := role_initiator.o role_responder.o app.o cfg.o timing.o slots.o \
         sync.o ranging.o output.o serial.o
OBJS  := $(LOCAL) $(FW)

all: run

%.o: %.c
	$(CC) $(CFLAGS) -c -o $@ $<
%.o: $(SRC)/%.c
	$(CC) $(CFLAGS) -c -o $@ $<

harness: $(OBJS)
	$(CC) -o $@ $(OBJS) -lm

run: harness
	./harness

clean:
	rm -f harness *.o *.d

.PHONY: all run clean

-include $(OBJS:.o=.d)
