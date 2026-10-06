import {
  Button,
  Card,
  Field,
  Link,
  makeStyles,
  mergeClasses,
  Spinner,
  Text,
  Textarea,
  tokens,
} from "@fluentui/react-components"
import { type KeyboardEvent, useCallback, useEffect, useRef, useState } from "react"
import { api, ReauthRequiredError } from "./api/client"
import type { components } from "./api/schema"

type Answer = components["schemas"]["Answer"]
type Ticket = components["schemas"]["Ticket"]
type UserProfile = components["schemas"]["UserProfile"]

const ASK_ERROR = "Could not get an answer. Try again."
const TICKET_ERROR = "Could not open a ticket. Try again."
const PROFILE_ERROR = "Could not load your profile."

const useStyles = makeStyles({
  root: {
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalL,
    maxWidth: "56rem",
    minHeight: "100dvh",
    marginInline: "auto",
    paddingBlock: tokens.spacingVerticalXL,
    paddingInline: tokens.spacingHorizontalL,
    overflowWrap: "anywhere",
  },
  header: {
    display: "flex",
    flexWrap: "wrap",
    alignItems: "center",
    justifyContent: "space-between",
    gap: tokens.spacingHorizontalM,
  },
  title: {
    margin: 0,
    fontSize: "clamp(1.25rem, 1rem + 1vw, 1.75rem)",
    lineHeight: tokens.lineHeightBase600,
    fontWeight: tokens.fontWeightSemibold,
  },
  heading: {
    margin: 0,
    fontSize: tokens.fontSizeBase400,
    lineHeight: tokens.lineHeightBase400,
    fontWeight: tokens.fontWeightSemibold,
  },
  workspace: {
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalL,
    minWidth: 0,
  },
  feed: {
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalL,
    minWidth: 0,
  },
  surface: {
    padding: tokens.spacingHorizontalL,
    borderRadius: tokens.borderRadiusXLarge,
    border: `1px solid ${tokens.colorNeutralStroke2}`,
    boxShadow: tokens.shadow2,
    minWidth: 0,
    flexShrink: 0,
    overflow: "visible",
  },
  feedArticle: {
    "&:focus-visible": {
      outlineWidth: "2px",
      outlineStyle: "solid",
      outlineColor: tokens.colorStrokeFocus2,
      outlineOffset: "2px",
    },
  },
  answer: {
    minHeight: "12rem",
  },
  answerText: {
    whiteSpace: "pre-wrap",
    lineHeight: tokens.lineHeightBase400,
    maxWidth: "70ch",
  },
  citations: {
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalS,
    paddingInlineStart: tokens.spacingHorizontalXL,
    marginBlock: 0,
  },
  actions: {
    display: "flex",
    flexWrap: "wrap",
    gap: tokens.spacingHorizontalS,
    alignItems: "center",
  },
  pendingIndicator: {
    "@media (prefers-reduced-motion: reduce)": {
      animationName: "none",
      animationDuration: "0s",
      animationIterationCount: "1",
      "&::before, &::after": { animationName: "none", animationDuration: "0s" },
    },
  },
  muted: { color: tokens.colorNeutralForeground2 },
  status: { minHeight: tokens.lineHeightBase300, color: tokens.colorNeutralForeground2 },
  escalation: {
    display: "flex",
    flexDirection: "column",
    gap: tokens.spacingVerticalS,
    paddingBlock: tokens.spacingVerticalS,
  },
  desktop: {
    "@media (min-width: 48rem) and (min-height: 44rem)": {
      height: "100dvh",
      minHeight: "44rem",
      "& main": {
        flex: "1 1 auto",
        minHeight: 0,
        overflowY: "auto",
        padding: tokens.spacingHorizontalXS,
        scrollbarGutter: "stable",
        borderRadius: tokens.borderRadiusXLarge,
      },
    },
  },
})

/**
 * The knowledge base may echo a source twice in one answer. Identical
 * (title, url) pairs render identically and would collide as React keys,
 * so keep the first occurrence; a different URL is a distinct source.
 */
function uniqueCitations(citations: Answer["citations"]): Answer["citations"] {
  const seen = new Set<string>()
  const unique: Answer["citations"] = []
  for (const citation of citations) {
    const key = citationKey(citation)
    if (seen.has(key)) continue
    seen.add(key)
    unique.push(citation)
  }
  return unique
}

/** The content-derived identity of a rendered citation: its (title, url) pair. */
function citationKey(citation: Answer["citations"][number]): string {
  return JSON.stringify([citation.title, citation.url])
}

const FEED_ARTICLE_SELECTOR = "[data-feed-index]"

const FOCUSABLE_AFTER_FEED_SELECTOR =
  "a[href], button:not([disabled]), textarea, input:not([type='hidden']), select, [tabindex]:not([tabindex='-1'])"

/**
 * Move focus to the first focusable element after (or, for `after = false`,
 * the last focusable element before) the feed, the APG feed pattern's exit
 * convention for Control+End / Control+Home.
 */
function focusFeedExit(feed: HTMLElement, after: boolean): void {
  const candidates = Array.from(
    document.querySelectorAll<HTMLElement>(FOCUSABLE_AFTER_FEED_SELECTOR),
  ).filter((el) => el !== feed && !feed.contains(el))
  let exit: HTMLElement | null = null
  if (after) {
    exit =
      candidates.find((el) =>
        Boolean(feed.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING),
      ) ?? null
  } else {
    for (let i = candidates.length - 1; i >= 0; i -= 1) {
      const candidate = candidates[i]
      if (candidate && candidate.compareDocumentPosition(feed) & Node.DOCUMENT_POSITION_FOLLOWING) {
        exit = candidate
        break
      }
    }
  }
  exit?.focus()
}

export function App({
  signedIn = false,
  initialAuthError = false,
  signIn,
  signOut,
  continueAsGuest,
}: {
  /** True when an account existed once the redirect callback resolved. */
  signedIn?: boolean
  /** True when the login redirect itself failed; opens on the recovery choice. */
  initialAuthError?: boolean
  signIn?: () => void
  signOut?: () => void
  continueAsGuest?: () => void
}) {
  const styles = useStyles()
  const [profile, setProfile] = useState<UserProfile | null>(null)
  const [guest, setGuest] = useState(false)
  const [authError, setAuthError] = useState(initialAuthError)
  // null while the profile loads; the kinds are distinguished so the
  // consent case can say exactly what is missing instead of a shrug.
  const [profileError, setProfileError] = useState<"consent" | "failed" | null>(null)
  const [question, setQuestion] = useState("")
  const [answer, setAnswer] = useState<Answer | null>(null)
  const [asking, setAsking] = useState(false)
  const [ticket, setTicket] = useState<Ticket | null>(null)
  const [escalating, setEscalating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [status, setStatus] = useState("Ready for your question.")
  // A deliberate guest or sign-out choice must hold: a profile fetch in
  // flight when the choice was made must not repaint a signed-in identity.
  // Each fetch captures the epoch; the choice bumps it and a stale result
  // is dropped.
  const profileEpoch = useRef(0)
  const guestRef = useRef(false)
  const feedRef = useRef<HTMLDivElement | null>(null)
  const answerFocusPending = useRef(false)

  // Reads only refs and setters, so it is stable: the mount effect runs
  // once, and the retry button re-runs it on demand.
  const loadProfile = useCallback(() => {
    if (guestRef.current) return
    const current = ++profileEpoch.current
    // Sign-in is optional: the API answers this anonymous with the guest profile.
    api.GET("/me").then(
      ({ data, error: body, response }) => {
        if (profileEpoch.current !== current) return
        if (response.ok) {
          setProfile(data ?? null)
          setProfileError(null)
        } else if (response.status === 503 && body?.code === "consent_required") {
          setProfileError("consent")
        } else {
          setProfileError("failed")
        }
      },
      (err: unknown) => {
        if (profileEpoch.current !== current) return
        if (err instanceof ReauthRequiredError) setAuthError(true)
        else setProfileError("failed")
      },
    )
  }, [])

  useEffect(() => {
    loadProfile()
  }, [loadProfile])

  // After a successful ask, move focus to the answer article: it is the
  // turn the asker waits for (APG feed pattern, app-driven focus). Only
  // a rendered answer consumes the pending flag; an empty one leaves it
  // for the next answer.
  useEffect(() => {
    if (!answer || !answerFocusPending.current) return
    answerFocusPending.current = false
    feedRef.current?.querySelector<HTMLElement>('[data-feed-index="2"]')?.focus()
  }, [answer])

  const chooseGuest = () => {
    continueAsGuest?.()
    profileEpoch.current += 1
    guestRef.current = true
    setProfile(null)
    setGuest(true)
    setAuthError(false)
    setProfileError(null)
    setError(null)
  }

  const chooseSignOut = () => {
    profileEpoch.current += 1
    guestRef.current = true
    setProfile(null)
    setProfileError(null)
    // logoutRedirect navigates the whole page; the local clears only keep
    // the render honest for the moment before the navigation lands.
    signOut?.()
  }

  // APG feed roving: Arrow/PageUp/Down step between articles, Home/End
  // jumps first/last, Ctrl+Home/End exits. No article is a tab stop:
  // Tab walks the articles' own controls; only editor keys pass through.
  const onFeedKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const feed = feedRef.current
    if (!feed) return
    const target = event.target as HTMLElement | null
    if (target?.closest("textarea, input, select")) return
    const current = target?.closest<HTMLElement>(FEED_ARTICLE_SELECTOR)
    if (!current) return
    const articles = Array.from(feed.querySelectorAll<HTMLElement>(FEED_ARTICLE_SELECTOR))
    const currentIndex = articles.indexOf(current)
    if (currentIndex === -1) return
    const focusArticle = (index: number) => {
      const clamped = Math.max(0, Math.min(articles.length - 1, index))
      articles[clamped]?.focus()
    }
    if ((event.ctrlKey || event.metaKey) && event.key === "Home") {
      event.preventDefault()
      focusFeedExit(feed, false)
      return
    }
    if ((event.ctrlKey || event.metaKey) && event.key === "End") {
      event.preventDefault()
      focusFeedExit(feed, true)
      return
    }
    if (event.ctrlKey || event.metaKey || event.altKey) return
    switch (event.key) {
      case "ArrowDown":
      case "PageDown":
        event.preventDefault()
        focusArticle(currentIndex + 1)
        return
      case "ArrowUp":
      case "PageUp":
        event.preventDefault()
        focusArticle(currentIndex - 1)
        return
      case "Home":
        event.preventDefault()
        focusArticle(0)
        return
      case "End":
        event.preventDefault()
        focusArticle(articles.length - 1)
        return
    }
  }

  const ask = async () => {
    if (!question.trim() || asking) return
    setAsking(true)
    setStatus("Finding an answer…")
    setAnswer(null)
    setError(null)
    try {
      const { data, response } = await api.POST("/answers", { body: { question } })
      if (response.ok) {
        answerFocusPending.current = true
        setAnswer(data ?? null)
        setStatus(data ? "Answer ready." : "No answer returned.")
      } else {
        setError(ASK_ERROR)
        setStatus("")
      }
    } catch (err) {
      setStatus("")
      if (err instanceof ReauthRequiredError) setAuthError(true)
      else setError(ASK_ERROR)
    } finally {
      setAsking(false)
    }
  }

  const escalate = async () => {
    if (escalating) return
    setEscalating(true)
    setStatus("Opening your ticket…")
    setError(null)
    try {
      const { data, response } = await api.POST("/tickets", {
        body: { shortDescription: question.slice(0, 160) || "Support request" },
      })
      if (response.ok) {
        setTicket(data ?? null)
        setStatus(data ? `Ticket ${data.number} opened.` : "No ticket returned.")
      } else {
        setError(TICKET_ERROR)
        setStatus("")
      }
    } catch (err) {
      setStatus("")
      if (err instanceof ReauthRequiredError) setAuthError(true)
      else setError(TICKET_ERROR)
    } finally {
      setEscalating(false)
    }
  }

  return (
    <div className={mergeClasses(styles.root, styles.desktop)}>
      <header className={styles.header}>
        <h1 className={styles.title}>IT Support Assistant</h1>
        <div className={styles.actions}>
          {!authError &&
            // Only a bootstrap account with a fetched profile is an identity.
            (guest || !signedIn ? (
              <>
                <Text className={styles.muted}>Guest</Text>
                {signIn && (
                  <Button appearance="secondary" onClick={signIn}>
                    Sign in
                  </Button>
                )}
              </>
            ) : profile ? (
              <>
                <Text className={styles.muted}>{profile.displayName}</Text>
                {signOut && (
                  <Button appearance="secondary" onClick={chooseSignOut}>
                    Sign out
                  </Button>
                )}
              </>
            ) : profileError ? null : (
              // Signed in, profile still in flight: the identity waits for
              // the fetch rather than flashing a guest or an empty header.
              <Text className={styles.muted}>Loading your profile…</Text>
            ))}
        </div>
      </header>
      <main className={styles.workspace} aria-label="IT support workspace">
        {authError && (
          <Card className={styles.surface}>
            <Text block role="alert">
              Your signed-in session could not be renewed. Choose how to continue.
            </Text>
            <div className={styles.actions}>
              {signIn && (
                <Button appearance="primary" onClick={signIn}>
                  Sign in again
                </Button>
              )}
              <Button appearance="secondary" onClick={chooseGuest}>
                Continue as guest
              </Button>
              {signOut && (
                <Button appearance="secondary" onClick={chooseSignOut}>
                  Sign out
                </Button>
              )}
            </div>
          </Card>
        )}
        {profileError && (
          <Card className={styles.surface}>
            <Text block role="alert">
              {profileError === "consent"
                ? "Your profile needs a one-time admin consent for directory access. Ask your administrator to grant it, then try again."
                : PROFILE_ERROR}
            </Text>
            <div className={styles.actions}>
              <Button appearance="secondary" onClick={loadProfile}>
                Try again
              </Button>
            </div>
          </Card>
        )}
        {error && <Text role="alert">{error}</Text>}
        {/* The conversation itself is one APG feed; auth errors and alerts
            stay outside it so an alert is never read as a feed article. */}
        <div
          ref={feedRef}
          role="feed"
          aria-label="IT support conversation"
          className={styles.feed}
          onKeyDown={onFeedKeyDown}
        >
          <Card
            role="article"
            className={mergeClasses(styles.surface, styles.feedArticle)}
            aria-labelledby="question-heading"
            tabIndex={-1}
            aria-posinset={1}
            aria-setsize={3}
            data-feed-index={1}
          >
            <h2 id="question-heading" className={styles.heading}>
              Ask a question
            </h2>
            <Field label="Your IT question" hint="Get help from the IT knowledge base.">
              <Textarea
                value={question}
                onChange={(_, d) => setQuestion(d.value)}
                placeholder="For example: How do I reset my password?"
                rows={3}
                maxLength={2000}
              />
            </Field>
            <div className={styles.actions}>
              <Button appearance="primary" onClick={ask} disabled={asking || !question.trim()}>
                Ask
              </Button>
            </div>
          </Card>
          <Card
            role="article"
            className={mergeClasses(styles.surface, styles.answer, styles.feedArticle)}
            aria-labelledby="answer-heading"
            aria-describedby="answer-content"
            aria-busy={asking}
            tabIndex={-1}
            aria-posinset={2}
            aria-setsize={3}
            data-feed-index={2}
          >
            <h2 id="answer-heading" className={styles.heading}>
              Answer
            </h2>
            {answer ? (
              <>
                <Text block className={styles.answerText} id="answer-content">
                  {answer.text}
                </Text>
                {answer.citations.length > 0 && (
                  <>
                    <h3 className={styles.heading}>Sources</h3>
                    <ul className={styles.citations}>
                      {uniqueCitations(answer.citations).map((c) => (
                        <li key={citationKey(c)}>
                          {c.url ? (
                            <Link href={c.url} target="_blank" rel="noreferrer">
                              {c.title}
                            </Link>
                          ) : (
                            <Text>{c.title}</Text>
                          )}
                        </li>
                      ))}
                    </ul>
                  </>
                )}
              </>
            ) : (
              // The describedby target alternates with the answer text: one
              // of the two is always rendered, so the reference resolves.
              <Text className={styles.muted} id="answer-content">
                {asking
                  ? "Searching the knowledge base…"
                  : "Your answer and sources will appear here."}
              </Text>
            )}
          </Card>
          <article
            className={mergeClasses(styles.escalation, styles.feedArticle)}
            aria-labelledby="help-heading"
            tabIndex={-1}
            aria-posinset={3}
            aria-setsize={3}
            data-feed-index={3}
          >
            <h2 id="help-heading" className={styles.heading}>
              Need more help?
            </h2>
            <Text className={styles.muted}>Open an IT ticket if you need further support.</Text>
            <div className={styles.actions}>
              <Button appearance="secondary" onClick={escalate} disabled={escalating}>
                Escalate to IT
              </Button>
            </div>
            {ticket && (
              <Card className={styles.surface}>
                <Text weight="semibold">Ticket {ticket.number}</Text>
                <Text>{ticket.shortDescription}</Text>
                {ticket.url && (
                  <Link href={ticket.url} target="_blank" rel="noreferrer">
                    Open in the ticketing system
                  </Link>
                )}
              </Card>
            )}
          </article>
        </div>
      </main>
      <div className={styles.actions}>
        {(asking || escalating) && (
          <Spinner
            size="tiny"
            aria-hidden="true"
            spinner={{ className: styles.pendingIndicator }}
            spinnerTail={{ className: styles.pendingIndicator }}
          />
        )}
        <Text role="status" aria-live="polite" aria-atomic="true" className={styles.status}>
          {status}
        </Text>
      </div>
    </div>
  )
}
