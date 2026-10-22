---
name: artifact-finalization
description: Finish local task artifacts reliably by checking required paths, formats, and task-specific acceptance conditions before reporting completion.
---

# Artifact finalization

Use this checklist after producing the user's requested local deliverables.

1. Re-read the output requirements and list every required file or response field.
2. Check that each required path exists, is nonempty when appropriate, and uses the requested format.
3. Run the cheapest relevant local validation available, such as parsing JSON, opening an image or PDF, checking media duration, or executing the supplied test.
4. Fix validation failures that are within the task scope, then rerun the failed check.
5. Report completion only after the requested deliverables pass their task-specific checks. Preserve useful failure evidence when a check cannot be completed.

Keep validation proportional to the task. Do not replace the requested deliverable with a status note, and do not claim a check ran when it did not.
