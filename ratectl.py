"""Lynx-Tx frame-by-frame rate control: choose every frame's QP so the
receiver's buffer never runs late.

The RK3588 encoder runs in fixed-QP mode and obeys a new QP on the very next
frame (bench-tested on Save Tonight: 29 changes of 29 took effect at once).
This controller keeps the standard's model of the receiver's buffer for the
picture: each encoded frame's bits go in, and the picture's share of the
channel drains them at 1/fps of a second per frame. Data still queued when
it is due is late, so the buffer must never hold more than `slack` seconds
of channel. Before each frame the controller predicts what the frame will
cost at each QP and picks the finest QP that keeps the buffer on its target.

The cost model: bits = c * 2^(-QP / k). On Save Tonight +10 QP gave 4.9x
fewer bits, so k is about 4.4 (+4.4 QP halves the bits). c is learned from
every frame as it comes out, separately for keyframes and the rest.

Behaviour, like a broadcast encoder's:
  - coarser at once when a frame would overfill (attack), finer one step a
    frame (release), so quality never pumps;
  - a planned keyframe gets room made for it in the frames before;
  - when even the coarsest QP would not fit, skip the frame - the receiver
    repeats the previous one - rather than ever sending late.
"""
import math


class BufferRC:
    def __init__(self, rate_bps, fps=25.0, slack=0.6, gop=50,
                 qp_min=34, qp_max=51, qp_start=36, k=4.4,
                 target=0.3, ceiling=0.85, guard=False):
        self.R = float(rate_bps)
        self.fps = fps
        self.drain = self.R / fps                    # bits leaving per frame
        self.Bmax = self.R * slack                   # beyond this, late
        self.Bt = target * self.Bmax                 # where it should sit
        self.Bceil = ceiling * self.Bmax             # never plan above this
        self.gop = gop
        self.qp_min, self.qp_max, self.k = qp_min, qp_max, k
        self.qp = qp_start
        # Guard (hybrid) mode: the QP chosen is only a FLOOR - the encoder's
        # own rate control may go coarser, and we cannot see by how much. A
        # frame smaller than predicted may just mean it did, so it is weak
        # evidence; a frame bigger than predicted is strong evidence.
        self.guard = guard
        self.B = 0.0                                 # bits queued now
        self.inflight = {}                           # frame n -> (qp, predicted bits, key)
        # Complexity: bits at QP 0 equivalent. Seeded from the bench test
        # (QP 30: ~270 kbit P, ~670 kbit key at 4K) - learned from frame 1.
        self.cP = 270e3 * 2 ** (30 / k)
        self.cK = 670e3 * 2 ** (30 / k)
        self.since_key = 0
        self.skipped = 0
        # Look-ahead: complexity per unit of measured activity, learned like
        # cP/cK but divided by each frame's activity score.
        self.uP = None
        self.uK = None
        self.cx_of = {}                              # frame n -> (activity P, activity K)
        self.stats = {"frames": 0, "skips": 0, "max_B": 0.0, "qp_sum": 0}

    # qp_min 34: a scene change cannot be predicted, so the finest QP is
    # set so that even a busy frame arriving unannounced (a cut from a still
    # to concert footage) costs no more than about a third of the buffer.
    # Moving pictures at these rates sit around QP 36-40 anyway.

    # -- the cost model ------------------------------------------------------
    def cost(self, qp, key):
        return (self.cK if key else self.cP) * 2 ** (-qp / self.k)

    def qp_for(self, bits, key):
        c = self.cK if key else self.cP
        return self.k * math.log2(c / max(bits, 1.0))

    # -- per frame ---------------------------------------------------------------
    def choose(self, n, key=False, future=None):
        """QP for frame n, or None to skip it."""
        # Never wait for ever: a frame not heard back about within 8 frames
        # is booked at its predicted size. (Matching by timestamp broke once
        # when a clip was restarted, and the controller - believing every
        # frame still outstanding - jammed at QP 51.)
        for m in [m for m in self.inflight if m < n - 8]:
            q0, b0, k0 = self.inflight[m]
            self.done(m, b0, k0)
        pending = sum(b for _, b, _ in self.inflight.values())
        Bp = max(0.0, self.B + pending - self.drain * len(self.inflight))
        key = key or self.since_key + 1 >= self.gop
        target = self.Bt
        # Make room for the next planned keyframe in the frames before it.
        to_key = self.gop - self.since_key - 1
        if not key and 0 < to_key <= 12:
            k_bits = self.cost(self.qp + 2, True)
            target = min(target, max(0.0, self.Bceil - k_bits - self.drain))
        # Spend the drain, plus an eighth of the way back to target: gentle
        # steering. (A third made the QP swing 34-51 within seconds, and a
        # smeared frame among sharp ones looked like judder on pans.)
        allowed = self.drain + (target - Bp) / 8.0
        room = self.Bmax * 0.9 - Bp + self.drain       # what fits before trouble
        if self.cost(self.qp_max, key) > self.Bmax * 0.95 - Bp + self.drain:
            # Even the coarsest frame would make the stream late: skip.
            if not key:
                self.stats["skips"] += 1
                return None
            q = self.qp_max
        elif key and self.guard:
            # Guard: keyframes at the plain-cbr anchor (34) unless that
            # would not fit.
            q = self.qp_min
            while q < self.qp_max and self.cost(q, True) > room:
                q += 1
        elif key:
            # A keyframe at the running QP + 2: the frames after it are built
            # on it, so a mushy one spoils a whole GOP. Coarser only if it
            # would not otherwise fit.
            q = self.qp + 2
            while q < self.qp_max and self.cost(q, True) > room:
                q += 1
        else:
            planned = self.plan(n, key, future)
            q = planned if planned is not None else math.ceil(self.qp_for(max(allowed, 1.0), key))
            # At most +3 a frame, unless the buffer really is about to
            # overflow; at most -1 a frame on the way back.
            if q > self.qp + 3 and self.cost(self.qp + 3, False) <= room:
                q = self.qp + 3
            q = max(self.qp - 1, q)
        q = max(self.qp_min, min(self.qp_max, q))
        self.qp = q
        pred = self.cost(q, key)
        if future and self.uP is not None:
            aP, aK, _ = future[0]
            pred = ((self.uK or self.uP * self.cK / self.cP) * aK if key else self.uP * aP) * 2 ** (-q / self.k)
            self.cx_of[n] = (aP, aK)
        elif future:
            self.cx_of[n] = (future[0][0], future[0][1])
        self.inflight[n] = (q, pred, key)
        return q

    def plan(self, n, key, future):
        """Look-ahead: one steady QP for the coming frames.

        future: [(activity P, activity K, key?), ...] for frames n, n+1, ...
        (n first). Returns the finest QP at which the whole window fits:
        the buffer, run forward frame by frame, never passes the ceiling
        and ends near target. None until the activity scale is learned."""
        if self.uP is None or not future:
            return None
        uK = self.uK if self.uK is not None else self.uP * (self.cK / self.cP)
        pending = sum(b for _, b, _ in self.inflight.values())
        B0 = max(0.0, self.B + pending - self.drain * len(self.inflight))

        def fits(q):
            B = B0
            for i, (aP, aK, k) in enumerate(future):
                bits = (uK * aK if k else self.uP * aP) * 2 ** (-q / self.k)
                B = max(0.0, B + bits - self.drain)
                if B > self.Bceil:
                    return False
            return B <= self.Bt + 0.25 * (self.Bceil - self.Bt)

        for q in range(self.qp_min, self.qp_max + 1):
            if fits(q):
                return q
        return self.qp_max

    def done(self, n, bits, key):
        """Frame n came out of the encoder: bits, and whether it was a key."""
        qp, _, _ = self.inflight.pop(n, (self.qp, bits, key))
        self.B = max(0.0, self.B + bits - self.drain)
        # Learn in proportion: average in the logarithm, and no single frame
        # may move the estimate by more than x4 - one strobe flash is not
        # a new scene.
        c = bits * 2 ** (qp / self.k)

        def blend(old, new, w):
            if old is None:
                return new
            if self.guard and new < old:
                w = w / 6                   # smaller: perhaps just coarser
            return math.exp((1 - w) * math.log(old) + w * math.log(new))

        ax = self.cx_of.pop(n, None)
        if ax is not None:
            u = c / ax[1 if key else 0]
            if key:
                self.uK = blend(self.uK, u, 0.3)
            else:
                self.uP = blend(self.uP, u, 0.3)
        if key:
            c = min(max(c, self.cK / 4), self.cK * 4)
            self.cK = blend(self.cK, c, 0.4)
            self.since_key = 0
        else:
            c = min(max(c, self.cP / 4), self.cP * 4)
            self.cP = blend(self.cP, c, 0.4)
            self.since_key += 1
        self.stats["frames"] += 1
        self.stats["qp_sum"] += qp
        self.stats["max_B"] = max(self.stats["max_B"], self.B)

    def skipped_frame(self):
        """A skipped frame: the channel still drains for its slot."""
        self.B = max(0.0, self.B - self.drain)
        self.since_key += 1

    def slack_used(self):
        """Seconds of picture queued - what the buffer monitor would call
        slack - minus... i.e. the time in hand is slack - B/R."""
        return self.B / self.R
