# Meeting email templates

The three templates in use, how they are rendered, and how the content that fills
them is authored. Rendering is in Python (`kik-py-2`); the TypeScript template in
`templates/` is the original and cannot be executed on this host.

## 1. Shared summary email (`emailType: SUMMARY`)

Every resolved attendee gets this. One copy per person, addressed to that person
only — no CC, no BCC.

Structure, in order:

1. Greeting with the recipient's name
2. `Meeting summary` heading, meeting title and formatted date
3. **Purpose** — one paragraph, what the meeting was for
4. **Takeaways** — short bullets
5. **Decisions and priorities** *(optional, agent-authored)*
6. **Blockers / help needed** *(optional, agent-authored)*
7. **Detailed summary** — headings with bullets
8. CTA button to `https://delivery-desk-frontend.vercel.app`

Recipients with no assigned tasks receive this and nothing else. That is correct
and expected — no action is invented to fill the gap.

### The two optional sections

Both are agent-authored and only appear when the plan supplies them for an
`AGENT_AUTHORED` plan. They are always placed after Takeaways and before the
Detailed summary. Validation refuses them if they are not lists of non-empty
strings.

- **Decisions and priorities** — separates actual decisions from general
  discussion.
- **Blockers / help needed** — what is stuck and who could help. Suggested
  helpers are labelled as suggestions, not as new assignments.

A section for progress since the previous meeting was added and then
**deliberately removed** — it is gone from the renderer, not merely omitted from
the data. A plan that supplies it has it ignored, and the removed field is not
part of the supported set.

## 2. Action-item email (`emailType: ACTIONS`)

Sent **only** to people who have one or more `ownerType: OWNER` actions. It
contains the action cards and no shared summary body.

Structure:

1. Greeting
2. `To-do list and related updates` heading, title and date
3. **Action items** — one card per owned task: title, body, and the supporting
   quote labelled either *From the transcript* or *From the meeting summary*
4. **Related items** — items owned by someone else that are relevant. These are
   labelled `Related item — owned by <name>` and are never presented as the
   recipient's task
5. CTA button

Validation enforces that an `ACTIONS` copy has at least one owned action, that
every owned action title appears in the rendered body, and that a copy cannot
claim both assigned actions and no actions.

## 3. Status-update follow-up (`emailType: FOLLOW_UP`)

Rendered by `lib/followup_template.py`, used by the scheduled `followup-send`
command. Subject: `Status update requested: <meeting> — <date>`.

Structure:

1. Greeting
2. `Today's action-item update` heading, meeting title and date
3. An introduction paragraph
4. One card per previously shared task, labelled `ITEM n · PREVIOUSLY SHARED`,
   carrying the original title, the original body, and an update prompt
5. A closing line asking for a reply **in the recipient's own words**, making
   clear that a correction of ownership or scope is welcome and that this is a
   status request, not a new assignment or a claim that an item is overdue
6. CTA button

Structured reply fields ("Status / expected completion / blockers") were
deliberately removed from an earlier version. The reply is free-form.

## How the content is authored

The toolkit never writes prose. The assistant does:

- reads the transcript in ordered batches and the provider summary as stored;
- writes `sharedNotes` (purpose, takeaways, optional sections, detailed summary);
- writes each recipient's `actions` with `ownerType`, and `sourceReferences`
  carrying the **exact quote** from the transcript segment or provider summary;
- keeps tasks for people who were only mentioned separate from tasks owned by the
  recipient;
- preserves unclear names, ASR errors and ambiguous ownership instead of silently
  correcting them.

Every material claim and action carries a source reference, and `validate`
checks each quote as an exact substring of the cited source.

## Editorial modes

- `PRESERVE_PROVIDER` — reuse the provider's own summary text verbatim. The
  stored text, and the deterministically extracted sections, must match exactly;
  the provider's action-items section is deliberately excluded from the shared
  body.
- `AGENT_AUTHORED` — the assistant writes the shared notes. Optional sections are
  only available in this mode.

## Rules that apply to all three

- One recipient per submission. Never CC or BCC.
- The service account is excluded from recipients by config.
- Each rendered artifact must equal a fresh rendering of that recipient at send
  time, so a hand-edited preview cannot go out.
- No ad-hoc sender scripts. `send-review` (test only) and `deliver-attendees`
  (approved attendee path) are the only transports.
- Graph `202 Accepted` means accepted, not delivered.