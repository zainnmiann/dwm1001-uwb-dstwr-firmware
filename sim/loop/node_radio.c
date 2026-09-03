/**
 * @file    node_radio.c
 * @brief   radio.h implemented in the child, over the harness IPC.
 *
 * This is the only firmware module the harness replaces. role_initiator.c and
 * role_responder.c are linked unmodified and cannot tell the difference: every
 * blocking radio call becomes one request/reply round trip with the parent,
 * which owns virtual time.
 *
 * Local time is whatever the parent last told us. That is exactly right: a
 * node's own compute does not advance the shared timeline, so between two
 * round trips radio_systime_hi32() must be constant. The parent charges the
 * processing cost explicitly (see --proc-us) rather than letting host CPU time
 * leak into the model.
 */
#include "ipc.h"

#include "radio.h"
#include "timing.h"

#include <string.h>
#include <stdlib.h>

static int64_t          s_now_dtu;      /* this node's local time */
static int32_t          s_carrier;
static radio_quality_t  s_quality = { -800, -880, 40, 1024 };
static uint32_t         s_part_id;

void node_radio_set_part_id(uint32_t id) { s_part_id = id; }

static void trip(const ipc_req_t *rq, ipc_rep_t *rp)
{
    ipc_round_trip(rq, rp);
    s_now_dtu = rp->now_dtu;
}

/* --- time ----------------------------------------------------------------- */
uint32_t radio_systime_hi32(void)
{
    return (uint32_t)((uint64_t)s_now_dtu >> 8);
}

int32_t radio_hi32_diff_us(uint32_t a, uint32_t b)
{
    /* Identical to the target implementation: one hi32 unit is 4.0064 ns. */
    int32_t d = (int32_t)(a - b);

    return (int32_t)(((int64_t)d * 1000LL) / 249600LL);
}

uint64_t radio_tx_ts_from_hi32(uint32_t tx_hi32)
{
    /* Bit 0 masked off, exactly as radio.c does: dwt_setdelayedtrxtime()
     * writes DX_TIME at offset 1 because the low 9 bits are ignored. Antenna
     * delay is zero in the harness, so a recovered tau is the true flight
     * time up to device-time quantization. */
    return (((uint64_t)(tx_hi32 & 0xFFFFFFFEu)) << 8) & DTU_MASK_40;
}

/* --- transmit ------------------------------------------------------------- */
int radio_tx_at(const uint8_t *buf, uint16_t len, uint32_t tx_hi32)
{
    ipc_req_t rq;
    ipc_rep_t rp;

    if (len > IPC_MAX_FRAME) { return RADIO_TX_LATE; }

    memset(&rq, 0, sizeof(rq));
    rq.kind = REQ_TX;
    rq.hi32 = tx_hi32;
    rq.len  = len;
    memcpy(rq.buf, buf, len);

    trip(&rq, &rp);
    return (rp.kind == REP_TX_OK) ? RADIO_TX_OK : RADIO_TX_LATE;
}

void radio_tx_wait(void)
{
    ipc_req_t rq;
    ipc_rep_t rp;

    memset(&rq, 0, sizeof(rq));
    rq.kind = REQ_TXWAIT;
    trip(&rq, &rp);
}

void radio_idle(void)
{
    ipc_req_t rq;
    ipc_rep_t rp;

    memset(&rq, 0, sizeof(rq));
    rq.kind = REQ_IDLE;
    trip(&rq, &rp);
}

/* --- receive -------------------------------------------------------------- */
int radio_rx(radio_rx_t *rx, uint32_t timeout_us, bool delayed, uint32_t start_hi32)
{
    ipc_req_t rq;
    ipc_rep_t rp;

    /* The role loops only ever use the immediate form. */
    (void)delayed;
    (void)start_hi32;

    memset(&rq, 0, sizeof(rq));
    rq.kind      = REQ_RX;
    rq.until_dtu = s_now_dtu + (int64_t)us_to_dtu(timeout_us ? timeout_us : 1u);

    trip(&rq, &rp);

    if (rp.kind != REP_FRAME) { return RADIO_RX_TIMEOUT; }

    memset(rx, 0, sizeof(*rx));
    rx->len   = rp.len;
    rx->rx_ts = rp.rx_ts;
    memcpy(rx->buf, rp.buf, rp.len);

    s_carrier         = rp.carrier_int;
    s_quality.rx_dbm10 = rp.rx_dbm10;
    s_quality.fp_dbm10 = rp.fp_dbm10;

    return RADIO_RX_FRAME;
}

int32_t radio_carrier_integrator(void) { return s_carrier; }

void radio_quality(radio_quality_t *q) { *q = s_quality; }

/* --- the rest of the surface, stubbed ------------------------------------- */
bool     radio_init(const cfg_t *c)        { (void)c; return true; }
bool     radio_reconfigure(const cfg_t *c) { (void)c; return true; }
uint32_t radio_txpower_reg(void)           { return 0x0E082848u; }
bool     radio_txpower_from_otp(void)      { return false; }
uint16_t radio_ant_tx(void)                { return 0u; }
uint16_t radio_ant_rx(void)                { return 0u; }
uint32_t radio_part_id(void)               { return s_part_id; }
