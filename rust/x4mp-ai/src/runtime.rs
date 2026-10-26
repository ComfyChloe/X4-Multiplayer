//! Script runtime: one AI context per entity, a stack of script frames, wakes, calls, returns and
//! interrupts, in the engine's two phases.
//!
//! Engine model reproduced here (reports/REWRITE-AI-ORDERS.md section 3; decompile of 0x1066750,
//! 0x1075280 and `AIDirector::Move` 0x106d250, 2026-10-26):
//! - A context only runs when woken (start, `<wait>` timer, blocking action done, called script
//!   returned, interrupt). A run (`ExecState_Run` 0xb58bb0) executes actions until a blocking action
//!   yields.
//! - `run_script` pushes the callee; the callee's first run is a separate run. When it returns the
//!   caller is woken with the return values.
//! - **Delivery** (any time in the frame, e.g. inside the component walk): only *conditions* are
//!   evaluated. Global handlers (`<interrupts>`, listener `AICallStackContext`) are tested from the top
//!   frame down, in declaration order per frame; the first match is queued (wake flag 0x40). The
//!   blocking action's own `<interrupt>` (listener `AIScriptContext`, only while blocked) is tested
//!   with `AIScript_CheckInterruptHandlers` 0x1075280; a match sets flag 4 and later events are ignored
//!   until it ran. Conditions short-circuit, and a condition's `chance` is rolled before the condition
//!   is checked (one draw). In base AI no global handler condition has a chance; blocking-interrupt
//!   conditions with a chance exist in `move.seekenemies` (distress, plunder), `masstraffic.flee` and
//!   `masstraffic.police`.
//! - **Actions** run inside `AIDirector::Move` when the context's turn comes, through the interrupt
//!   runner 0xb58ce0: queued global handler actions first, then the blocking interrupt's actions,
//!   then the main `ExecState_Run`. `abort_called_scripts [resume=label]` aborts the frames above the
//!   handler's frame (their `<on_abort>` runs, top first) and restarts that frame at the label (default:
//!   the label passed last). A blocking interrupt that does not `<resume>` ends the blocking action.

use crate::orders::patrol::Patrol;
use crate::orders::seekenemies::SeekEnemies;
use crate::orders::targetselection::{TargetSelection, TsParams};
use crate::rng::Stream;
use crate::val::{Time, Uid, Val};
use crate::world::{Cx, Facts, Io, Stop, R};

/// Return values of a script (`<return value=>` / `<retval name= value=>`).
pub type RetVals = Vec<(String, Val)>;

/// Why a frame runs now.
#[derive(Clone, Debug, PartialEq)]
pub enum Wake {
    /// First run of a new frame.
    Start,
    /// A `<wait>` timed out.
    Timer,
    /// The blocking action finished (move_to arrived, or its interrupt matched without `resume`).
    Block,
    /// The called script returned.
    Returned(RetVals),
    /// Restart at a label after `abort_called_scripts` / an interrupt `<resume>`.
    Restart,
}

impl Wake {
    pub fn name(&self) -> &'static str {
        match self {
            Wake::Start => "start",
            Wake::Timer => "timer",
            Wake::Block => "block",
            Wake::Returned(_) => "return",
            Wake::Restart => "restart",
        }
    }
}

/// How a run ended.
#[derive(Clone, Debug, PartialEq)]
pub enum Yield {
    /// `<wait>`: until the game time (f64), or without timeout (`None`, only an interrupt ends it).
    Wait { until: Option<Time> },
    /// `<move_to>` issued (the effect was emitted); the frame blocks until it ends.
    MoveTo,
    /// `<run_script>` issued (the effect was emitted); the callee runs next.
    Call { script: &'static str, params: Vec<(&'static str, Val)> },
    /// The script ended (falls off its actions or `<return>`).
    Return(RetVals),
}

impl Yield {
    pub fn kind(&self) -> &'static str {
        match self {
            Yield::Wait { .. } => "wait",
            Yield::MoveTo => "moveto",
            Yield::Call { .. } => "call",
            Yield::Return(_) => "return",
        }
    }
}

/// An engine event as the script sees it (`event.name`, `event.object`, `event.param..3`).
#[derive(Clone, Debug, PartialEq)]
pub struct Event {
    pub name: String,
    pub object: Uid,
    pub param: Val,
    pub param2: Val,
    pub param3: Val,
}

/// Control outcome of handler or interrupt actions.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    /// Actions ran, no control change.
    Nothing,
    /// `abort_called_scripts`: abort frames above, restart the handler's frame at `label` (or at the
    /// label passed last when `None`).
    AbortCalled { label: Option<&'static str> },
    /// Blocking interrupt matched and its actions did not resume: the blocking action ends.
    BlockDone,
    /// Blocking interrupt actions executed `<resume label=>`.
    Resume { label: &'static str },
}

/// Engine verdict for a global-handler delivery (from the capture when conditions are opaque).
#[derive(Clone, Debug, PartialEq)]
pub enum GlobalVerdict {
    None,
    /// The global handler `handler` of frame `script` matched (and was queued).
    Handler {
        script: String,
        handler: String,
    },
}

/// Engine verdict for a blocking-interrupt delivery (`move.seekenemies` conditions are opaque in M1).
#[derive(Clone, Debug, PartialEq)]
pub enum BlockVerdict {
    /// Not offered (not blocked) or conditions false.
    None,
    /// Ignored because a blocking interrupt is already queued (flag 4).
    Ignored,
    /// Conditions held; `detected` is what they left in `$detected`.
    Matched { detected: Option<Vec<Uid>> },
}

/// A queued interrupt waiting for the context's turn in `AIDirector::Move`.
#[derive(Clone, Debug, PartialEq)]
pub struct Pending {
    /// Frame index (0 = bottom) the handler belongs to.
    pub depth: usize,
    /// Handler index in the frame's handler list; `None` = the blocking action's interrupt.
    pub handler: Option<usize>,
    pub ev: Event,
    /// Not ported: the replay takes its actions' effects from the capture.
    pub opaque: bool,
    pub detected: Option<Vec<Uid>>,
}

/// A script frame.
#[derive(Clone, Debug)]
pub enum Frame {
    Patrol(Box<Patrol>),
    Seek(Box<SeekEnemies>),
    TargetSel(Box<TargetSelection>),
    /// A script that is not ported; replay takes its runs from the capture.
    Opaque {
        script: String,
    },
}

impl Frame {
    pub fn name(&self) -> &str {
        match self {
            Frame::Patrol(_) => Patrol::NAME,
            Frame::Seek(_) => SeekEnemies::NAME,
            Frame::TargetSel(_) => TargetSelection::NAME,
            Frame::Opaque { script } => script,
        }
    }

    fn resume(&mut self, cx: &mut Cx<'_>, wake: Wake) -> R<Yield> {
        match self {
            Frame::Patrol(p) => p.resume(cx, wake),
            Frame::Seek(s) => s.resume(cx, wake),
            Frame::TargetSel(t) => t.resume(cx, wake),
            Frame::Opaque { script } => Err(Stop::Mismatch(format!("opaque frame {script} cannot run natively"))),
        }
    }

    pub fn handlers(&self) -> &'static [&'static str] {
        match self {
            Frame::Patrol(_) => Patrol::HANDLERS,
            Frame::Seek(_) => SeekEnemies::HANDLERS,
            Frame::TargetSel(_) | Frame::Opaque { .. } => &[],
        }
    }

    /// Conditions of global handler `i`: `Some(true/false)` when ported (never draws), `None` when
    /// opaque.
    pub fn matches(&self, facts: &dyn Facts, ent: Uid, i: usize, ev: &Event) -> Option<bool> {
        match self {
            Frame::Patrol(p) => p.matches(facts, i, ev),
            Frame::Seek(s) => s.matches(facts, ent, i, ev),
            Frame::TargetSel(_) | Frame::Opaque { .. } => None,
        }
    }

    fn handler_actions(&mut self, cx: &mut Cx<'_>, i: usize, ev: &Event) -> R<Outcome> {
        match self {
            Frame::Patrol(p) => p.handler_actions(cx, i, ev),
            Frame::Seek(s) => s.handler_actions(cx, i, ev),
            Frame::TargetSel(_) => Err(Stop::Mismatch("lib.target.selection has no handlers".into())),
            Frame::Opaque { script } => Err(Stop::Mismatch(format!("handler actions of opaque frame {script}"))),
        }
    }

    fn block_interrupt(&mut self, cx: &mut Cx<'_>, ev: &Event, detected: Option<&[Uid]>) -> R<Outcome> {
        match self {
            Frame::Seek(s) => s.block_interrupt(cx, ev, detected),
            Frame::Patrol(_) | Frame::TargetSel(_) => Err(Stop::Mismatch(format!(
                "{} has no blocking action with an interrupt (event {})",
                self.name(),
                ev.name
            ))),
            Frame::Opaque { script } => Err(Stop::Mismatch(format!("blocking interrupt of opaque frame {script}"))),
        }
    }

    /// Whether the frame is blocked in an action that has an `<interrupt>`.
    fn has_block_interrupt(&self) -> bool {
        match self {
            Frame::Seek(s) => s.blocked_with_interrupt(),
            _ => false,
        }
    }

    fn on_abort(&mut self, cx: &mut Cx<'_>) -> R<()> {
        match self {
            Frame::Seek(s) => s.on_abort(cx),
            _ => Ok(()),
        }
    }

    fn restart_at(&mut self, label: Option<&str>) -> R<()> {
        match self {
            Frame::Patrol(p) => p.restart_at(label),
            Frame::Seek(s) => s.restart_at(label),
            Frame::TargetSel(_) | Frame::Opaque { .. } => Ok(()),
        }
    }

    fn take_callee(&mut self) -> Option<Frame> {
        match self {
            Frame::Patrol(p) => p.take_callee(),
            _ => None,
        }
    }
}

/// What a frame waits for (decides the next [`Wake`]).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Waiting {
    /// Not started yet.
    Start,
    Timer,
    /// Blocked in a `move_to` that has not finished.
    Block,
    /// The blocking action finished (arrival or interrupt); due.
    BlockDone,
    Return,
    Restart,
    /// Finished (popped).
    Done,
}

/// One AI context (the pilot entity's script stack).
#[derive(Clone, Debug)]
pub struct Agent {
    pub ent: Uid,
    pub stack: Vec<Frame>,
    /// Per frame: what it waits for.
    pub waiting: Vec<Waiting>,
    /// Return values for a frame waiting on a returned call.
    pub returned: Option<RetVals>,
    pub rng: Stream,
    /// Timed wake of the top frame (for schedulers).
    pub wake_at: Option<Time>,
    /// Queued global handler interrupts (flag 0x40), in delivery order.
    pub pending_global: Vec<Pending>,
    /// Queued blocking interrupt (flag 4).
    pub pending_block: Option<Pending>,
    /// Set when the native behaviour stopped (unsupported branch); the original must run the order.
    pub stopped: Option<String>,
    /// Called scripts to keep opaque even when a native port exists (replay of traces that model them
    /// as engine runs, or a staged rollout).
    pub opaque: Vec<String>,
}

impl Agent {
    pub fn new(ent: Uid, rng_state: u64) -> Agent {
        Agent {
            ent,
            stack: Vec::new(),
            waiting: Vec::new(),
            returned: None,
            rng: Stream::from_state(rng_state),
            wake_at: None,
            pending_global: Vec::new(),
            pending_block: None,
            stopped: None,
            opaque: Vec::new(),
        }
    }

    pub fn top_name(&self) -> Option<&str> {
        self.stack.last().map(|f| f.name())
    }

    /// Start an order script (the `run_order_script` of orders.base). Runs its `<init>` now.
    pub fn start_order(&mut self, frame: Frame, facts: &dyn Facts, io: &mut dyn Io) -> R<()> {
        self.stack.clear();
        self.waiting.clear();
        self.returned = None;
        self.wake_at = None;
        self.pending_global.clear();
        self.pending_block = None;
        self.stopped = None;
        let mut frame = frame;
        if let Frame::Patrol(p) = &mut frame {
            let mut cx = Cx { ent: self.ent, facts, io, rng: &mut self.rng };
            p.init(&mut cx)?;
        }
        self.stack.push(frame);
        self.waiting.push(Waiting::Start);
        Ok(())
    }

    /// The [`Wake`] the top frame expects next.
    pub fn next_wake(&mut self) -> Option<Wake> {
        let w = *self.waiting.last()?;
        Some(match w {
            Waiting::Start => Wake::Start,
            Waiting::Timer => Wake::Timer,
            Waiting::Block | Waiting::BlockDone => Wake::Block,
            Waiting::Return => Wake::Returned(self.returned.take().unwrap_or_default()),
            Waiting::Restart => Wake::Restart,
            Waiting::Done => return None,
        })
    }

    /// Name of the wake the top frame expects, without consuming return values.
    pub fn next_wake_name(&self) -> &'static str {
        match self.waiting.last() {
            Some(Waiting::Start) => "start",
            Some(Waiting::Timer) => "timer",
            Some(Waiting::Block | Waiting::BlockDone) => "block",
            Some(Waiting::Return) => "return",
            Some(Waiting::Restart) => "restart",
            Some(Waiting::Done) | None => "none",
        }
    }

    /// Run the top (native) frame once.
    pub fn run(&mut self, facts: &dyn Facts, io: &mut dyn Io) -> R<Yield> {
        let wake = self.next_wake().ok_or_else(|| Stop::Mismatch("run of an agent without frames".into()))?;
        let top = self.stack.len() - 1;
        let y = {
            let mut cx = Cx { ent: self.ent, facts, io, rng: &mut self.rng };
            self.stack[top].resume(&mut cx, wake)?
        };
        self.after_yield(&y);
        Ok(y)
    }

    /// Bookkeeping after the top frame yielded (also used when replay applies an opaque frame's yield).
    pub fn after_yield(&mut self, y: &Yield) {
        let top = self.stack.len() - 1;
        self.wake_at = None;
        match y {
            Yield::Wait { until } => {
                self.waiting[top] = Waiting::Timer;
                self.wake_at = *until;
            }
            Yield::MoveTo => self.waiting[top] = Waiting::Block,
            Yield::Call { script, params } => {
                self.waiting[top] = Waiting::Return;
                let forced = self.opaque.iter().any(|s| s == script);
                let native = self.stack[top].take_callee();
                let callee = if forced {
                    Frame::Opaque { script: script.to_string() }
                } else if let Some(f) = native {
                    f
                } else if *script == TargetSelection::NAME {
                    let ps: Vec<(String, Val)> = params.iter().map(|(k, v)| (k.to_string(), v.clone())).collect();
                    match TsParams::from_params(&ps) {
                        Ok(p) => Frame::TargetSel(Box::new(TargetSelection::new(p, self.ship_of_top(top)))),
                        Err(_) => Frame::Opaque { script: script.to_string() },
                    }
                } else {
                    Frame::Opaque { script: script.to_string() }
                };
                self.stack.push(callee);
                self.waiting.push(Waiting::Start);
            }
            Yield::Return(vals) => {
                self.stack.pop();
                self.waiting.pop();
                if let Some(w) = self.waiting.last_mut() {
                    *w = Waiting::Return;
                    self.returned = Some(vals.clone());
                }
            }
        }
    }

    /// The ship of the frame at `depth` (callees act for the same ship).
    fn ship_of_top(&self, depth: usize) -> Uid {
        match &self.stack[depth] {
            Frame::Patrol(p) => p.ship(),
            Frame::Seek(s) => s.ship(),
            Frame::TargetSel(t) => t.ship(),
            Frame::Opaque { .. } => Uid::NULL,
        }
    }

    /// Replay: an opaque frame called another (opaque) script.
    pub fn call_opaque(&mut self, script: &str) {
        let top = self.stack.len() - 1;
        self.waiting[top] = Waiting::Return;
        self.wake_at = None;
        self.stack.push(Frame::Opaque { script: script.to_string() });
        self.waiting.push(Waiting::Start);
    }

    /// Abort the whole order script (orders.base `abort_called_scripts`): `<on_abort>` top first.
    pub fn abort_all(&mut self, facts: &dyn Facts, io: &mut dyn Io) -> R<()> {
        while let Some(mut f) = self.stack.pop() {
            self.waiting.pop();
            let mut cx = Cx { ent: self.ent, facts, io, rng: &mut self.rng };
            f.on_abort(&mut cx)?;
        }
        self.returned = None;
        self.wake_at = None;
        self.pending_global.clear();
        self.pending_block = None;
        Ok(())
    }

    /// Abort the frames above `depth` (top first) and restart frame `depth` at `label`.
    pub fn abort_above(&mut self, depth: usize, label: Option<&str>, facts: &dyn Facts, io: &mut dyn Io) -> R<()> {
        while self.stack.len() > depth + 1 {
            let mut f = self.stack.pop().expect("frame");
            self.waiting.pop();
            let mut cx = Cx { ent: self.ent, facts, io, rng: &mut self.rng };
            f.on_abort(&mut cx)?;
        }
        // Interrupts queued for the aborted frames are gone with them.
        self.pending_global.retain(|p| p.depth <= depth);
        if self.pending_block.as_ref().is_some_and(|p| p.depth > depth) {
            self.pending_block = None;
        }
        self.returned = None;
        self.wake_at = None;
        self.stack[depth].restart_at(label)?;
        self.waiting[depth] = Waiting::Restart;
        Ok(())
    }

    /// Delivery to the global handlers (conditions only). Native conditions are evaluated; the
    /// engine `verdict` decides for opaque ones and is checked against the native ones.
    /// Returns the matched (script, handler) if any.
    pub fn deliver_global(
        &mut self,
        ev: &Event,
        verdict: &GlobalVerdict,
        facts: &dyn Facts,
    ) -> R<Option<(String, String)>> {
        for depth in (0..self.stack.len()).rev() {
            let script = self.stack[depth].name().to_string();
            for (i, h) in self.stack[depth].handlers().iter().enumerate() {
                let named =
                    matches!(verdict, GlobalVerdict::Handler { script: s, handler: n } if *s == script && n == h);
                match self.stack[depth].matches(facts, self.ent, i, ev) {
                    Some(true) => {
                        if !named {
                            return Err(Stop::Mismatch(format!(
                                "native {script}/{h} matches {}, engine verdict is {verdict:?}",
                                ev.name
                            )));
                        }
                        self.pending_global.push(Pending {
                            depth,
                            handler: Some(i),
                            ev: ev.clone(),
                            opaque: false,
                            detected: None,
                        });
                        return Ok(Some((script, h.to_string())));
                    }
                    Some(false) => {
                        if named {
                            return Err(Stop::Mismatch(format!(
                                "engine says {script}/{h} matched {}, native conditions are false",
                                ev.name
                            )));
                        }
                    }
                    None => {
                        if named {
                            self.pending_global.push(Pending {
                                depth,
                                handler: Some(i),
                                ev: ev.clone(),
                                opaque: true,
                                detected: None,
                            });
                            return Ok(Some((script, h.to_string())));
                        }
                    }
                }
            }
        }
        match verdict {
            GlobalVerdict::None => Ok(None),
            // A frame below the native stack (orders.base): handled by the replay as opaque.
            GlobalVerdict::Handler { script, handler } if script == "orders.base" => {
                Ok(Some((script.clone(), handler.clone())))
            }
            GlobalVerdict::Handler { script, handler } => {
                Err(Stop::Mismatch(format!("verdict names {script}/{handler}, which is not on the native stack")))
            }
        }
    }

    /// Delivery to the blocking action's `<interrupt>` (conditions are opaque in M1: the engine's
    /// verdict is applied, after checking that the native state allows it).
    pub fn deliver_block(&mut self, ev: &Event, verdict: &BlockVerdict) -> R<()> {
        let top = match self.stack.len().checked_sub(1) {
            Some(t) => t,
            None => return Err(Stop::Mismatch(format!("{} delivered to an agent without frames", ev.name))),
        };
        match verdict {
            BlockVerdict::None => Ok(()),
            BlockVerdict::Ignored => {
                if self.pending_block.is_none() {
                    return Err(Stop::Mismatch(
                        "engine ignored a blocking interrupt but none is queued natively".into(),
                    ));
                }
                Ok(())
            }
            BlockVerdict::Matched { detected } => {
                if self.pending_block.is_some() {
                    return Err(Stop::Mismatch("second blocking interrupt while one is queued (flag 4)".into()));
                }
                if !self.stack[top].has_block_interrupt() {
                    return Err(Stop::Mismatch(format!(
                        "engine matched a blocking interrupt but native {} is not blocked in one",
                        self.stack[top].name()
                    )));
                }
                self.pending_block = Some(Pending {
                    depth: top,
                    handler: None,
                    ev: ev.clone(),
                    opaque: false,
                    detected: detected.clone(),
                });
                Ok(())
            }
        }
    }

    /// The context's turn in `AIDirector::Move`: run the first queued global handler's actions.
    /// Returns the frame name and outcome; opaque handlers return `None` (the caller applies the
    /// captured outcome with [`Agent::apply_opaque_abort`]).
    pub fn run_global_interrupt(&mut self, facts: &dyn Facts, io: &mut dyn Io) -> R<Option<(String, Outcome)>> {
        if self.pending_global.is_empty() {
            return Err(Stop::Mismatch("interrupt run without a queued global interrupt".into()));
        }
        let p = self.pending_global.remove(0);
        if p.opaque {
            return Ok(None);
        }
        let i = p.handler.expect("global handler index");
        let script = self.stack[p.depth].name().to_string();
        let out = {
            let mut cx = Cx { ent: self.ent, facts, io: &mut *io, rng: &mut self.rng };
            self.stack[p.depth].handler_actions(&mut cx, i, &p.ev)?
        };
        if let Outcome::AbortCalled { label } = out {
            self.abort_above(p.depth, label, facts, io)?;
        }
        Ok(Some((script, out)))
    }

    /// Apply an opaque handler's `abort_called_scripts` at frame `depth` (from the capture).
    pub fn apply_opaque_abort(
        &mut self,
        depth: usize,
        label: Option<&str>,
        facts: &dyn Facts,
        io: &mut dyn Io,
    ) -> R<()> {
        self.abort_above(depth, label, facts, io)
    }

    /// The context's turn: run the queued blocking interrupt's actions.
    pub fn run_block_interrupt(&mut self, facts: &dyn Facts, io: &mut dyn Io) -> R<(String, Outcome)> {
        let p = self
            .pending_block
            .take()
            .ok_or_else(|| Stop::Mismatch("interrupt run without a queued blocking interrupt".into()))?;
        let script = self.stack[p.depth].name().to_string();
        let out = {
            let mut cx = Cx { ent: self.ent, facts, io, rng: &mut self.rng };
            self.stack[p.depth].block_interrupt(&mut cx, &p.ev, p.detected.as_deref())?
        };
        match out {
            Outcome::BlockDone => {
                self.waiting[p.depth] = Waiting::BlockDone;
                self.wake_at = None;
            }
            Outcome::Resume { label } => {
                self.stack[p.depth].restart_at(Some(label))?;
                self.waiting[p.depth] = Waiting::Restart;
                self.wake_at = None;
            }
            _ => {}
        }
        Ok((script, out))
    }
}
