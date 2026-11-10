# fix/test-short-freeze

## 1. Summary
Test-only fix. `test_short_freeze_is_recorded_as_internal_gap` failed intermittently while the
machine was busy (seen on several branches). The recording software was behaving correctly; the
test asked for something that did not happen.

## 2. Requirements
E3 (gap detection) test reliability. No product code changed.

## 3. Cause (independent review, confirmed by the fix's behaviour)
The simulated camera drops what its real-time encoder produces during a wall-clock freeze. Under
heavy load the encoder falls behind, so a 2.5 s freeze could remove < 1 s of *stream* time, which
is correctly below `max_internal_gap_s = 1.0` and therefore not a gap. The test then timed out.

## 4. Change and tests
Freeze for 5 s and assert 1.0 s < gap <= 5.5 s. Ran the test 5 times in a row while other work
was loading the machine: **5/5 passed**.

## 5. Related product observation (not fixed here)
The reviewer noted (PLAUSIBLE, untested) that `finalizer.py` treats a shortfall of up to ~1.5 GOP
in segment 0 as FFmpeg's start-up lead-in without checking the segment's first PTS, so a real
timestamp jump of that size at the end of segment 0 would not become a gap row. Passed to the
time-accuracy branch, which owns timestamp derivation.

## 7. Recommendation
**Merge.**
