/**
 * @file    uart_io.h
 * @brief   Non-blocking UART console for the DWM1001-DEV J-Link VCOM.
 *
 * TX is a ring buffer drained by the UART interrupt, so range reports never
 * stall the TDMA state machine -- a 115200 baud line takes ~87 us per byte,
 * which is a quarter of an anchor response sub-slot.  If the ring overflows,
 * the bytes that did not fit are dropped and a counter is bumped rather than
 * blocking -- the queued backlog is preserved, the newest output is lost.
 */

#ifndef UART_IO_H_
#define UART_IO_H_

#include <stdint.h>
#include <stdbool.h>

/** TX ring size; must be a power of two. Sized for the burst a full Report
 *  group produces at N_R = 12 before the line drains. */
#define UART_TX_RING_SZ     4096u
/** Longest accepted command line. $ANCHOR carries eight fields. */
#define UART_RX_LINE_SZ     160u

void     uart_io_init(uint32_t baud);

/** True if the nRF52 UART can actually produce this rate. Rates outside the
 *  hardware's enumerated set would otherwise run at a different speed than the
 *  configuration reports. */
bool     uart_io_baud_supported(uint32_t baud);
void     uart_io_write(const char *data, uint32_t len);
void     uart_io_puts(const char *s);

/** printf-style, formatted into a stack buffer then queued. No float support. */
void     uart_io_printf(const char *fmt, ...);

/** Block until the TX ring has drained. Only for shutdown paths. */
void     uart_io_flush(void);

/** Number of bytes dropped because the TX ring was full. */
uint32_t uart_io_dropped(void);

/** Poll for a completed input line. Returns NULL until '\n' or '\r' arrives. */
char    *uart_io_poll_line(void);

#endif /* UART_IO_H_ */
