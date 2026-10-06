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
import { useEffect, useRef, useState } from "react"
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
    "&:focus-visible": {
      outline: `2px solid ${tokens.colorStrokeFocus2}`,
      outlineOffset: "-2px",
    },
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

export function App({
  signedIn = false,
  signIn,
  signOut,
  continueAsGuest,
}: {
  /** True when an account existed at bootstrap; the profile fetch confirms it. */
  signedIn?: boolean
  signIn?: () => void
  signOut?: () => void
  continueAsGuest?: () => void
}) {
  const styles = useStyles()
  const [profile, setProfile] = useState<UserProfile | null>(null)
  const [guest, setGuest] = useState(false)
  const [authError, setAuthError] = useState(false)
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

  useEffect(() => {
    if (guestRef.current) return
    const current = ++profileEpoch.current
    // Sign-in is optional: the API answers this anonymous with the guest profile.
    api.GET("/me").then(
      ({ data, response }) => {
        if (profileEpoch.current !== current) return
        if (response.ok) setProfile(data ?? null)
        else setError(PROFILE_ERROR)
      },
      (err: unknown) => {
        if (profileEpoch.current !== current) return
        if (err instanceof ReauthRequiredError) setAuthError(true)
        else setError(PROFILE_ERROR)
      },
    )
  }, [])

  const chooseGuest = () => {
    continueAsGuest?.()
    profileEpoch.current += 1
    guestRef.current = true
    setProfile(null)
    setGuest(true)
    setAuthError(false)
    setError(null)
  }

  const chooseSignOut = () => {
    profileEpoch.current += 1
    guestRef.current = true
    setProfile(null)
    // logoutRedirect navigates the whole page; the local clears only keep
    // the render honest for the moment before the navigation lands.
    signOut?.()
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
            ) : null)}
        </div>
      </header>
      <main className={styles.workspace} tabIndex={0} aria-label="IT support workspace">
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
        {error && <Text role="alert">{error}</Text>}
        <Card role="region" className={styles.surface} aria-labelledby="question-heading">
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
          role="region"
          className={mergeClasses(styles.surface, styles.answer)}
          aria-labelledby="answer-heading"
          aria-busy={asking}
        >
          <h2 id="answer-heading" className={styles.heading}>
            Answer
          </h2>
          {answer ? (
            <>
              <Text block className={styles.answerText}>
                {answer.text}
              </Text>
              {answer.citations.length > 0 && (
                <>
                  <h3 className={styles.heading}>Sources</h3>
                  <ul className={styles.citations}>
                    {answer.citations.map((c, index) => (
                      <li key={`${index}-${c.title}`}>
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
            <Text className={styles.muted}>
              {asking ? "Searching the knowledge base…" : "Your answer and sources will appear here."}
            </Text>
          )}
        </Card>
        <section className={styles.escalation} aria-labelledby="help-heading">
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
        </section>
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
