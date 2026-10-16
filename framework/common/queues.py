"""Queue names, the visibility timeouts and the allowed reply targets.

9 consumed queues + 8 dead-letter queues = 17 queues (kept services only: catalogue and geometry). Names are lowercase [a-z0-9_], at most 47 characters.
"""

SERVICES = ("catalogue", "geometry")

ORCH_IN, ORCH_REPLY = "orch_in", "orch_reply"
CATALOGUE_Q, GW_REPLY, CHECK_REPLY = "catalogue_q", "gw_reply", "svc_check_reply"


def cmd(svc):
    return "%s_cmd" % svc


def ctl(svc):
    return "%s_ctl" % svc


def dead(queue):
    return "%s_dead" % queue


CONSUMED = (ORCH_IN, ORCH_REPLY) + tuple(cmd(s) for s in SERVICES) + tuple(ctl(s) for s in SERVICES) \
    + (CATALOGUE_Q, GW_REPLY, CHECK_REPLY)
DEAD = tuple(dead(q) for q in CONSUMED if q != CHECK_REPLY)
ALL = CONSUMED + DEAD

# vt in seconds when a queue is read: commands 30 (extended while they run), the quick queues 10.
VT_S = {ORCH_IN: 30, ORCH_REPLY: 30}
VT_S.update({cmd(s): 30 for s in SERVICES})
VT_S.update({ctl(s): 10 for s in SERVICES})
VT_S.update({CATALOGUE_Q: 10, GW_REPLY: 10, CHECK_REPLY: 10})

REPLY_TO_ALLOWED = (ORCH_REPLY, GW_REPLY, CHECK_REPLY)

assert len(CONSUMED) == 9 and len(DEAD) == 8 and len(ALL) == 17
