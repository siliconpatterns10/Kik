import type { MeetingSummarySections } from './meeting-summary-sections';

export interface MeetingActionEmailTask {
  title: string;
  body: string;
  dueDate: Date | null;
  evidenceQuotes: string[];
}

export interface MeetingActionEmailInput {
  recipientName: string;
  meetingTitle: string;
  meetingDate: Date | null;
  meetingSummary: MeetingSummarySections;
  reviewCopyFor?: string;
  tasks: MeetingActionEmailTask[];
  appUrl: string;
}

export interface MeetingActionEmailTemplate {
  subject: string;
  text: string;
  html: string;
}

const MAX_QUOTE_LENGTH = 420;
const MISSING_PURPOSE = 'A purpose was not included in the saved meeting notes.';
const MISSING_TAKEAWAYS = 'No takeaways were included in the saved meeting notes.';
const MISSING_DETAIL = 'No detailed summary was included in the saved meeting notes.';

/** Outlook-safe meeting notes and recipient-specific action items. */
export function renderMeetingActionEmail(
  input: MeetingActionEmailInput,
): MeetingActionEmailTemplate {
  const reviewCopyFor = input.reviewCopyFor?.trim() || null;
  const subject = safeHeader(
    `${reviewCopyFor ? `[Review copy for ${reviewCopyFor}] ` : ''}Meeting notes: ${input.meetingTitle}`,
  );
  const meetingDate = formatDate(input.meetingDate);
  const appUrl = safeHttpUrl(input.appUrl);
  const greeting = input.recipientName.trim() || 'there';
  const summary = input.meetingSummary;

  const taskText = input.tasks.length
    ? input.tasks.map((task, index) => {
        const due = task.dueDate ? `\nDue: ${formatDate(task.dueDate)}` : '';
        const evidence = task.evidenceQuotes.length
          ? `\nFrom the meeting: “${truncate(task.evidenceQuotes[0], MAX_QUOTE_LENGTH)}”`
          : '';
        return `${index + 1}. ${task.title}\n${task.body}${due}${evidence}`;
      })
    : ['No action items were assigned to you in the saved analysis.'];

  const detailedText = summary.detailedSummary.length
    ? summary.detailedSummary.flatMap(({ heading, points }) => [
        ...(heading ? [heading] : []),
        ...points.map((point) => `• ${point}`),
        '',
      ])
    : [MISSING_DETAIL];
  const text = [
    `Hello ${greeting},`,
    '',
    'Meeting notes',
    `${input.meetingTitle}${meetingDate ? ` · ${meetingDate}` : ''}`,
    ...(reviewCopyFor
      ? [`Review copy for ${reviewCopyFor}: these notes and action items are for review and are not assigned to you.`]
      : []),
    '',
    'Purpose',
    summary.purpose ?? MISSING_PURPOSE,
    '',
    'Takeaways',
    ...(summary.takeaways.length ? summary.takeaways.map((item) => `• ${item}`) : [MISSING_TAKEAWAYS]),
    '',
    'Detailed summary',
    ...detailedText,
    'Action items',
    ...taskText,
    '',
    appUrl ? `Open Delivery Desk: ${appUrl}` : 'Open Delivery Desk from your usual app link.',
    '',
    reviewCopyFor
      ? `Review copy for ${reviewCopyFor}; the listed action items belong to them.`
      : 'This digest is private to you. If an assignment looks wrong, contact the meeting organizer.',
  ].join('\n');

  const taskRows = input.tasks.length
    ? input.tasks
        .map((task, index) => {
          const due = task.dueDate
            ? `<p style="margin:14px 0 0;color:#5b6070;font:14px/22px Arial,sans-serif;"><strong style="color:#14151a;">Due</strong> &nbsp;${escapeHtml(formatDate(task.dueDate))}</p>`
            : '';
          const evidence = task.evidenceQuotes.length
            ? `<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin-top:16px;border-left:3px solid #c6f24e;"><tr><td style="padding:2px 0 2px 12px;color:#5b6070;font:13px/20px Arial,sans-serif;"><strong style="color:#14151a;">From the meeting</strong><br>&ldquo;${escapeHtml(truncate(task.evidenceQuotes[0], MAX_QUOTE_LENGTH))}&rdquo;</td></tr></table>`
            : '';
          return `<tr><td style="padding:${index === 0 ? '0' : '14px'} 0 0;"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border:1px solid #e4e4de;border-radius:12px;"><tr><td style="padding:18px 20px 20px;"><p style="margin:0 0 7px;color:#5b6070;font:12px/18px Arial,sans-serif;letter-spacing:.04em;text-transform:uppercase;">Action ${index + 1}</p><h3 style="margin:0;color:#14151a;font:700 18px/25px Arial,sans-serif;letter-spacing:-.02em;">${escapeHtml(task.title)}</h3><p style="margin:9px 0 0;color:#333744;font:15px/23px Arial,sans-serif;">${escapeHtml(task.body).replace(/\n/g, '<br>')}</p>${due}${evidence}</td></tr></table></td></tr>`;
        })
        .join('')
    : `<tr><td style="padding:0;color:#5b6070;font:15px/23px Arial,sans-serif;">No action items were assigned to you in the saved analysis.</td></tr>`;

  const takeawaysHtml = summary.takeaways.length
    ? listHtml(summary.takeaways)
    : `<p style="margin:0;color:#5b6070;font:15px/23px Arial,sans-serif;">${MISSING_TAKEAWAYS}</p>`;
  const detailedSummaryHtml = summary.detailedSummary.length
    ? summary.detailedSummary
        .map(({ heading, points }) => `<div style="margin:${heading ? '0 0 16px' : '0'};">${heading ? `<h3 style="margin:0 0 6px;color:#14151a;font:700 16px/23px Arial,sans-serif;">${escapeHtml(heading)}</h3>` : ''}${listHtml(points)}</div>`)
        .join('')
    : `<p style="margin:0;color:#5b6070;font:15px/23px Arial,sans-serif;">${MISSING_DETAIL}</p>`;
  const appLink = appUrl
    ? `<table role="presentation" cellspacing="0" cellpadding="0" border="0"><tr><td bgcolor="#c6f24e" style="border-radius:999px;"><a href="${escapeHtml(appUrl)}" style="display:inline-block;padding:12px 20px;color:#14151a;text-decoration:none;font:600 14px/20px Arial,sans-serif;">Open Delivery Desk</a></td></tr></table>`
    : '';
  const reviewNoticeHtml = reviewCopyFor
    ? `<tr><td class="dd-email-pad" style="padding:0 30px 20px;"><p style="margin:0;padding:12px 14px;border-left:3px solid #9bc900;background:#f1f7df;color:#333744;font:14px/21px Arial,sans-serif;"><strong>Review copy for ${escapeHtml(reviewCopyFor)}.</strong> The notes and action items below are not assigned to you.</p></td></tr>`
    : '';
  const footerNote = reviewCopyFor
    ? `Review copy for ${reviewCopyFor}; the listed action items belong to them.`
    : 'This digest is private to you. If an assignment looks wrong, contact the meeting organizer.';
  const html = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <style type="text/css">
      @media only screen and (max-width: 480px) {
        .dd-email-pad { padding-left:20px !important; padding-right:20px !important; }
        .dd-brand-cell, .dd-follow-up-cell { display:block !important; width:100% !important; text-align:left !important; }
        .dd-follow-up-cell { padding-top:10px !important; }
      }
    </style>
  </head>
  <body bgcolor="#f1f1ec" style="margin:0;padding:0;background:#f1f1ec;color:#14151a;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="#f1f1ec" style="background:#f1f1ec;">
      <tr>
        <td align="center" style="padding:28px 12px;">
          <!--[if mso]><table role="presentation" width="620" cellspacing="0" cellpadding="0" border="0"><tr><td><![endif]-->
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="#fafaf7" style="max-width:620px;background:#fafaf7;border:1px solid #e4e4de;border-radius:16px;">
            <tr>
              <td class="dd-email-pad" style="padding:22px 30px;border-bottom:1px solid #e4e4de;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                  <tr>
                    <td class="dd-brand-cell" style="color:#14151a;font:700 17px/24px Arial,sans-serif;">Delivery Desk</td>
                    <td class="dd-follow-up-cell" align="right"><span style="display:inline-block;padding:5px 11px;border-radius:999px;background:#c6f24e;color:#14151a;font:600 11px/16px Arial,sans-serif;">MEETING NOTES</span></td>
                  </tr>
                </table>
              </td>
            </tr>
            <tr>
              <td class="dd-email-pad" style="padding:27px 30px 18px;">
                <p style="margin:0 0 10px;color:#5b6070;font:14px/21px Arial,sans-serif;">Hello ${escapeHtml(greeting)},</p>
                <h1 style="margin:0;color:#14151a;font:700 28px/34px Arial,sans-serif;letter-spacing:-.025em;">Meeting notes</h1>
                <p style="margin:8px 0 0;color:#5b6070;font:15px/23px Arial,sans-serif;"><strong style="color:#14151a;">${escapeHtml(input.meetingTitle)}</strong>${meetingDate ? ` &nbsp;·&nbsp; ${escapeHtml(meetingDate)}` : ''}</p>
              </td>
            </tr>
            ${reviewNoticeHtml}
            ${summarySectionHtml('Purpose', `<p style="margin:0;color:#333744;font:15px/24px Arial,sans-serif;">${escapeHtml(summary.purpose ?? MISSING_PURPOSE)}</p>`)}
            ${summarySectionHtml('Takeaways', takeawaysHtml)}
            ${summarySectionHtml('Detailed summary', detailedSummaryHtml)}
            <tr>
              <td class="dd-email-pad" style="padding:0 30px 24px;">
                <h2 style="margin:0 0 13px;color:#14151a;font:700 20px/27px Arial,sans-serif;">Action items</h2>
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">${taskRows}</table>
              </td>
            </tr>
            <tr>
              <td class="dd-email-pad" style="padding:0 30px 28px;">
                ${appLink}
                <p style="margin:18px 0 0;color:#5b6070;font:13px/20px Arial,sans-serif;">${escapeHtml(footerNote)}</p>
              </td>
            </tr>
          </table>
          <!--[if mso]></td></tr></table><![endif]-->
          <p style="margin:14px 0 0;color:#5b6070;font:12px/18px Arial,sans-serif;">Delivery Desk &middot; Silicon Patterns</p>
        </td>
      </tr>
    </table>
  </body>
</html>`;

  return { subject, text, html };
}

function summarySectionHtml(title: string, body: string): string {
  return `<tr><td class="dd-email-pad" style="padding:0 30px 23px;"><h2 style="margin:0 0 9px;color:#14151a;font:700 20px/27px Arial,sans-serif;">${title}</h2>${body}</td></tr>`;
}

function listHtml(items: string[]): string {
  if (!items.length) return '';
  return `<ul style="margin:0;padding:0 0 0 20px;color:#333744;font:15px/24px Arial,sans-serif;">${items.map((item) => `<li style="padding:0 0 5px 2px;">${escapeHtml(item)}</li>`).join('')}</ul>`;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function safeHeader(value: string): string {
  return value
    .replace(/[\r\n\u0000-\u001f\u007f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 180);
}

function truncate(value: string, limit: number): string {
  const compact = value.replace(/\s+/g, ' ').trim();
  return compact.length <= limit ? compact : `${compact.slice(0, limit - 1)}…`;
}

function formatDate(value: Date | null): string {
  if (!value || Number.isNaN(value.getTime())) return '';
  return new Intl.DateTimeFormat('en-IN', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(value);
}

function safeHttpUrl(value: string): string | null {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.toString() : null;
  } catch {
    return null;
  }
}
