// The user's own note for a meeting — the one thing in this app the user writes
// rather than an agent.
//
// A plain textarea, not a rich editor: the content is markdown (it renders through
// the dashboard's shared renderer elsewhere), and during a meeting the useful
// affordance is typing without the cursor jumping, which every WYSIWYG layer
// eventually breaks.
//
// Saving is DEBOUNCED and automatic. A meeting note has no natural moment to press
// Save — the meeting is the moment — and a note lost because the user closed the
// panel mid-thought is the exact failure this feature exists to prevent. The flush
// on unmount is what covers closing the panel, ending the meeting, and navigating
// away.

import { useCallback, useEffect, useRef, useState } from 'react'
import { NotebookPen, X } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import { Btn } from '../../../components/ui'

/** Quiet period after the last keystroke before a save fires. */
const SAVE_DEBOUNCE_MS = 800

interface Props {
  /** Server content. Used to seed the editor and to adopt an external change. */
  content: string
  updatedAt: string
  saving: boolean
  onSave: (content: string) => void
  onClose: () => void
}

export default function NoteSidebar({ content, updatedAt, saving, onSave, onClose }: Props) {
  const [draft, setDraft] = useState(content)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // What the server last confirmed. Compared on flush so an unmount cannot re-save
  // text nobody changed, and compared below to decide whether a server update is
  // genuinely external.
  const savedRef = useRef(content)
  const draftRef = useRef(content)
  draftRef.current = draft
  const onSaveRef = useRef(onSave)
  onSaveRef.current = onSave

  // Adopt a server value that differs from what we last sent — another tab, or the
  // first load landing after the panel opened. Guarded on `savedRef` rather than on
  // `draft`, or every keystroke would be reverted by the in-flight response.
  useEffect(() => {
    if (content === savedRef.current) return
    savedRef.current = content
    setDraft(content)
  }, [content])

  const flush = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
    if (draftRef.current === savedRef.current) return
    savedRef.current = draftRef.current
    onSaveRef.current(draftRef.current)
  }, [])

  // Flush on unmount: closing the panel or ending the meeting must not drop the
  // last few seconds of typing.
  useEffect(() => () => { flush() }, [flush])

  const change = (value: string) => {
    setDraft(value)
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(flush, SAVE_DEBOUNCE_MS)
  }

  const dirty = draft !== savedRef.current

  return (
    <aside
      className="flex-none w-[340px] border-l border-border bg-bg flex flex-col overflow-hidden"
      aria-label={i18nT('apps.meetings.note.title')}
    >
      <div className="flex-none px-3 py-2.5 border-b border-border flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <NotebookPen className="lucide-inline text-muted" />
          <span className="text-[13px] font-semibold text-text-strong truncate">
            {i18nT('apps.meetings.note.title')}
          </span>
        </div>
        <Btn onClick={onClose} aria-label={i18nT('apps.meetings.note.close')}>
          <X className="lucide-inline" />
        </Btn>
      </div>

      <textarea
        value={draft}
        onChange={e => change(e.target.value)}
        onBlur={flush}
        placeholder={i18nT('apps.meetings.note.placeholder')}
        // Distinct from the <aside>'s label on purpose: the region and the control
        // are different things, and giving both the same accessible name makes them
        // indistinguishable to a screen reader (and ambiguous to a test).
        aria-label={i18nT('apps.meetings.note.editorLabel')}
        spellCheck
        className="flex-1 min-h-0 resize-none bg-transparent border-none outline-none p-3 text-[13px] leading-relaxed text-text font-body placeholder:text-muted/60"
      />

      <div className="flex-none px-3 py-2 border-t border-border text-[12px] text-muted">
        {/* Three states, because "did my note save?" is the only question a user
            asks of an autosaving field. */}
        {saving
          ? i18nT('apps.meetings.note.saving')
          : dirty
            ? i18nT('apps.meetings.note.unsaved')
            : updatedAt
              ? i18nT('apps.meetings.note.saved')
              : i18nT('apps.meetings.note.hint')}
      </div>
    </aside>
  )
}
