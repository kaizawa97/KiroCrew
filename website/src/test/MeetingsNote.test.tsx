// The meeting-note panel.
//
// Everything worth testing here is about NOT LOSING what the user typed, since the
// note is the one thing in this app they cannot regenerate: the debounce must not
// swallow the last keystrokes, an in-flight save must not revert the field, and
// closing the panel must flush rather than discard.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'

import NoteSidebar from '../apps/meetings/components/NoteSidebar'
import EN_CATALOG from '../i18n/locales/en.json'

const SessionSource = readFileSync('src/apps/meetings/hooks/useMeetingSession.ts', 'utf-8')
const StoreSource = readFileSync(
  '../src/kiro_crew/apps/builtins/meetings/backend/store.py', 'utf-8',
)

const NOTE = EN_CATALOG.apps.meetings.note

function setup(over: Partial<Parameters<typeof NoteSidebar>[0]> = {}) {
  const onSave = vi.fn()
  const onClose = vi.fn()
  const view = render(
    <NoteSidebar
      content=""
      updatedAt=""
      saving={false}
      onSave={onSave}
      onClose={onClose}
      {...over}
    />,
  )
  // The editor's own label, distinct from the region's — see NoteSidebar.
  const field = screen.getByLabelText(NOTE.editorLabel) as HTMLTextAreaElement
  // `fireEvent.change`, not a hand-dispatched input event: React's internal value
  // tracker suppresses onChange when `.value` is assigned directly, so the naive
  // version silently never reaches the component.
  const type = (value: string) => {
    act(() => { fireEvent.change(field, { target: { value } }) })
  }
  return { view, onSave, onClose, field, type }
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('NoteSidebar', () => {
  it('seeds the field from the server value', () => {
    const { field } = setup({ content: 'ship on Friday' })
    expect(field.value).toBe('ship on Friday')
  })

  it('does not save on every keystroke', () => {
    // One request per character would hammer the endpoint for a whole meeting.
    const { onSave, type } = setup()
    type('a')
    type('ab')
    expect(onSave).not.toHaveBeenCalled()
  })

  it('saves once the typing stops', () => {
    const { onSave, type } = setup()
    type('decision: ship')
    act(() => { vi.advanceTimersByTime(1000) })
    expect(onSave).toHaveBeenCalledTimes(1)
    expect(onSave).toHaveBeenCalledWith('decision: ship')
  })

  it('restarts the debounce while typing continues', () => {
    const { onSave, type } = setup()
    type('a')
    act(() => { vi.advanceTimersByTime(500) })
    type('ab')
    act(() => { vi.advanceTimersByTime(500) })
    expect(onSave).not.toHaveBeenCalled()
    act(() => { vi.advanceTimersByTime(400) })
    expect(onSave).toHaveBeenCalledWith('ab')
  })

  it('flushes on unmount, so closing the panel cannot drop the last words', () => {
    // The failure this exists for: type, close, lose the sentence.
    const { view, onSave, type } = setup()
    type('half a thought')
    view.unmount()
    expect(onSave).toHaveBeenCalledWith('half a thought')
  })

  it('flushes on blur', () => {
    const { onSave, field, type } = setup()
    type('clicked away')
    act(() => { fireEvent.blur(field) })
    expect(onSave).toHaveBeenCalledWith('clicked away')
  })

  it('does not re-save unchanged text on unmount', () => {
    const { view, onSave } = setup({ content: 'untouched' })
    view.unmount()
    expect(onSave).not.toHaveBeenCalled()
  })

  it('saves an empty note, because clearing it is a real edit', () => {
    const { onSave, type } = setup({ content: 'delete me' })
    type('')
    act(() => { vi.advanceTimersByTime(1000) })
    expect(onSave).toHaveBeenCalledWith('')
  })

  it('does NOT revert the field when the in-flight save echoes back', () => {
    // The classic autosave bug: the response for "ab" lands while the user has
    // typed "abcd", and adopting it blindly rewinds their cursor and their text.
    const { view, field, type, onSave } = setup()
    type('ab')
    act(() => { vi.advanceTimersByTime(1000) })
    expect(onSave).toHaveBeenCalledWith('ab')
    type('abcd')
    view.rerender(
      <NoteSidebar
        content="ab"
        updatedAt="2026-08-04T00:00:00Z"
        saving={false}
        onSave={onSave}
        onClose={() => {}}
      />,
    )
    expect(field.value).toBe('abcd')
  })

  it('adopts a genuinely external change', () => {
    // Another tab, or the first load landing after the panel opened.
    const { view, field, onSave } = setup({ content: '' })
    view.rerender(
      <NoteSidebar
        content="written elsewhere"
        updatedAt="2026-08-04T00:00:00Z"
        saving={false}
        onSave={onSave}
        onClose={() => {}}
      />,
    )
    expect(field.value).toBe('written elsewhere')
  })

  it('answers "did it save?" in each of its three states', () => {
    const idle = setup({ content: 'x', updatedAt: '2026-08-04T00:00:00Z' })
    expect(screen.getByText(NOTE.saved)).toBeTruthy()
    idle.view.unmount()

    const busy = setup({ saving: true })
    expect(screen.getByText(NOTE.saving)).toBeTruthy()
    busy.view.unmount()

    const dirty = setup()
    dirty.type('typing')
    expect(screen.getByText(NOTE.unsaved)).toBeTruthy()
  })
})

describe('note wiring', () => {
  it('is not polled', () => {
    // The textarea is the authoritative copy; refetching under the user is how an
    // autosaving editor loses a sentence.
    const block = SessionSource.match(/const noteQuery = useQuery\(\{[\s\S]*?\n {2}\}\)/)
    expect(block).toBeTruthy()
    expect(block![0]).toContain('refetchInterval: false')
    expect(block![0]).toContain('refetchOnWindowFocus: false')
    expect(block![0]).toContain('noteOpen')
  })

  it('seeds the cache from the save response instead of invalidating', () => {
    // An invalidate would refetch and hand the editor a value mid-keystroke.
    const block = SessionSource.match(/const noteMutation = useMutation\(\{[\s\S]*?\n {2}\}\)/)
    expect(block).toBeTruthy()
    expect(block![0]).toContain('setQueryData')
    expect(block![0]).not.toContain('invalidateQueries')
  })
})

describe('the note filename cannot be owned by an agent', () => {
  it('is documented at the store, not just in the constant', () => {
    // Agent outputs share the meeting directory and are named from the agent id.
    // The leading underscore is what makes this path unreachable by that
    // derivation; the Python side pins it, and this is the frontend-side reminder
    // that the filename is a security property rather than a style choice.
    expect(StoreSource).toContain('k.NOTE_FILE')
    expect(StoreSource).toContain('un-ownable by any agent')
  })
})
