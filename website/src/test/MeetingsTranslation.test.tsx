// The live-translation side panel and the wiring that feeds it.
//
// The panel is rendered directly (its props are pure data), and the parts that
// live inside the session hook — incremental cursor accumulation, and the gate
// that stops polling for a feature nobody enabled — are pinned against the
// shipping source, the technique MeetingsSessionLogic.test.ts established for
// hook internals that cannot be rendered in isolation.

import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'

import TranslationSidebar from '../apps/meetings/components/TranslationSidebar'
import type { TranslationLine } from '../apps/meetings/api'
import EN_CATALOG from '../i18n/locales/en.json'

const SessionSource = readFileSync('src/apps/meetings/hooks/useMeetingSession.ts', 'utf-8')
const ViewSource = readFileSync('src/apps/meetings/MeetingView.tsx', 'utf-8')
const ApiSource = readFileSync('src/apps/meetings/api.ts', 'utf-8')

const line = (n: number, source: string, text: string): TranslationLine => ({ n, source, text })

const renderPanel = (over: Partial<Parameters<typeof TranslationSidebar>[0]> = {}) =>
  render(
    <TranslationSidebar
      lines={[]}
      languageLabel="日本語"
      pending={0}
      dropped={0}
      loading={false}
      onClose={() => {}}
      {...over}
    />,
  )

describe('TranslationSidebar', () => {
  it('shows the target language as its own endonym', () => {
    // Not translated on purpose: a reader looking for Japanese recognises 日本語.
    renderPanel()
    expect(screen.getByText('日本語')).toBeTruthy()
  })

  it('shows the source line beside its translation', () => {
    // Both halves, because the panel exists for someone who only partly follows the
    // meeting — seeing them together is what lets them check a doubtful translation
    // against what was actually said.
    renderPanel({ lines: [line(0, 'we ship on Friday', '金曜日にリリースします')] })
    expect(screen.getByText('we ship on Friday')).toBeTruthy()
    expect(screen.getByText('金曜日にリリースします')).toBeTruthy()
  })

  it('marks a failed line instead of dropping it', () => {
    // An empty translation is persisted precisely so the line is not a silent gap
    // the user cannot tell apart from "nobody spoke".
    renderPanel({ lines: [line(0, 'we ship on Friday', '')] })
    expect(screen.getByText('we ship on Friday')).toBeTruthy()
    expect(
      screen.getByText(EN_CATALOG.apps.meetings.translation.lineFailed),
    ).toBeTruthy()
  })

  it('renders lines in spoken order', () => {
    renderPanel({
      lines: [line(0, 'first', 'un'), line(1, 'second', 'deux'), line(2, 'third', 'trois')],
    })
    const body = document.body.textContent ?? ''
    expect(body.indexOf('un')).toBeLessThan(body.indexOf('deux'))
    expect(body.indexOf('deux')).toBeLessThan(body.indexOf('trois'))
  })

  it('explains an empty panel differently while loading', () => {
    const idle = renderPanel({ loading: false })
    expect(
      idle.getByText(EN_CATALOG.apps.meetings.translation.emptyHint),
    ).toBeTruthy()
    idle.unmount()

    const busy = renderPanel({ loading: true })
    expect(busy.getByText(EN_CATALOG.apps.meetings.translation.loading)).toBeTruthy()
  })

  it('says it is catching up rather than looking stuck', () => {
    // Translation runs one line at a time behind live speech, so a backlog is normal.
    renderPanel({ lines: [line(0, 'a', 'b')], pending: 7 })
    expect(screen.getByText(EN_CATALOG.apps.meetings.translation.pending)).toBeTruthy()
  })

  it('reports dropped lines, because that is data loss', () => {
    renderPanel({ lines: [line(0, 'a', 'b')], dropped: 3 })
    expect(screen.getByText(EN_CATALOG.apps.meetings.translation.dropped)).toBeTruthy()
  })

  it('hides the status footer when there is nothing to report', () => {
    renderPanel({ lines: [line(0, 'a', 'b')] })
    expect(screen.queryByText(EN_CATALOG.apps.meetings.translation.pending)).toBeNull()
    expect(screen.queryByText(EN_CATALOG.apps.meetings.translation.dropped)).toBeNull()
  })
})

describe('the incremental poll', () => {
  it('sends a cursor rather than refetching the whole document', () => {
    // A long meeting accumulates hundreds of lines and the panel polls while open;
    // resending all of them every few seconds would grow linearly for no gain.
    expect(ApiSource).toContain('translations: (id: string, since = 0)')
    expect(ApiSource).toContain('/translations?since=')
    expect(SessionSource).toContain(
      'meetingsApi.translations(meetingId, translationCursorRef.current)',
    )
    expect(SessionSource).toContain('translationCursorRef.current = page.next_n')
  })

  it('accumulates into a Map keyed by line number, not an array', () => {
    // `queryFn` appending would duplicate every line if it ran twice for one cursor,
    // which React Strict Mode's double-invoke does in development. Keying by `n`
    // makes the merge idempotent.
    expect(SessionSource).toContain('new Map<number, TranslationLine>()')
    expect(SessionSource).toContain('translationLinesRef.current.set(line.n, line)')
  })

  it('resets when the target language changes', () => {
    // The backend starts a fresh document, so keeping the old lines would show a mix
    // with no way to tell which line is in which language.
    expect(SessionSource).toContain('lastTranslationLanguageRef')
    expect(SessionSource).toMatch(/if \(page\.language !== lastTranslationLanguageRef\.current\)/)
  })

  it('polls only while the panel is open AND a language is set', () => {
    // Translation is off by default; polling for it regardless would be pure waste.
    const enabled = SessionSource.match(/enabled: initQuery\.isSuccess && [^\n]*/)
    expect(enabled).toBeTruthy()
    expect(enabled![0]).toContain('translationOpen')
    expect(enabled![0]).toContain('Boolean(translationLanguage)')
  })
})

describe('MeetingView wiring', () => {
  it('offers the toggle only when a language is configured', () => {
    // With translation off the button would open a panel that can never fill.
    expect(ViewSource).toMatch(/\{translation\.language && \(\s*<Btn/)
  })

  it('mounts the panel only when open and configured', () => {
    expect(ViewSource).toContain('{translation.open && translation.language && (')
  })

  it('takes the language label from the server, not a client-side list', () => {
    // The backend publishes the accepted languages and their endonyms, so a second
    // copy in the client would be the thing that drifts.
    expect(ViewSource).toContain('languageLabel={translation.languageLabel}')
    expect(SessionSource).toContain('page.language_label')
  })
})
