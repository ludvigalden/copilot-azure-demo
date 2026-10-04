import {
  Button,
  Card,
  CardHeader,
  Link,
  makeStyles,
  Spinner,
  Text,
  Textarea,
  Title2,
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
    gap: "16px",
    maxWidth: "720px",
    margin: "0 auto",
    padding: "24px 16px",
  },
  header: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
  },
  citations: {
    display: "flex",
    flexDirection: "column",
    gap: "4px",
    marginTop: "8px",
  },
  actions: {
    display: "flex",
    gap: "8px",
    alignItems: "center",
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
    setAnswer(null)
    setError(null)
    try {
      const { data, response } = await api.POST("/answers", { body: { question } })
      if (response.ok) setAnswer(data ?? null)
      else setError(ASK_ERROR)
    } catch (err) {
      if (err instanceof ReauthRequiredError) setAuthError(true)
      else setError(ASK_ERROR)
    } finally {
      setAsking(false)
    }
  }

  const escalate = async () => {
    if (escalating) return
    setEscalating(true)
    setError(null)
    try {
      const { data, response } = await api.POST("/tickets", {
        body: { shortDescription: question.slice(0, 160) || "Support request" },
      })
      if (response.ok) setTicket(data ?? null)
      else setError(TICKET_ERROR)
    } catch (err) {
      if (err instanceof ReauthRequiredError) setAuthError(true)
      else setError(TICKET_ERROR)
    } finally {
      setEscalating(false)
    }
  }

  return (
    <div className={styles.root}>
      <div className={styles.header}>
        <Title2>IT Support Assistant</Title2>
        <div className={styles.actions}>
          {!authError &&
            // An anonymous caller and a deliberate guest both wear the guest
            // label: only a bootstrap account with a fetched profile is an
            // identity, and only that identity can be signed out.
            (guest || !signedIn ? (
              <>
                <Text weight="semibold">Guest</Text>
                {signIn && (
                  <Button appearance="secondary" onClick={signIn}>
                    Sign in
                  </Button>
                )}
              </>
            ) : profile ? (
              <>
                <Text weight="semibold">{profile.displayName}</Text>
                {signOut && (
                  <Button appearance="secondary" onClick={chooseSignOut}>
                    Sign out
                  </Button>
                )}
              </>
            ) : null)}
        </div>
      </div>
      {authError && (
        <Card>
          <Text block>Your signed-in session could not be renewed. Choose how to continue.</Text>
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
      <Textarea
        value={question}
        onChange={(_, d) => setQuestion(d.value)}
        placeholder="Ask an IT question, e.g. How do I reset my password?"
        rows={3}
        maxLength={2000}
      />
      <div className={styles.actions}>
        <Button appearance="primary" onClick={ask} disabled={asking || !question.trim()}>
          Ask
        </Button>
        <Button appearance="secondary" onClick={escalate} disabled={escalating}>
          Escalate to IT
        </Button>
        {(asking || escalating) && <Spinner size="tiny" />}
      </div>
      {answer && (
        <Card>
          <CardHeader
            header={<Text weight="semibold">Answer</Text>}
            description={<Text size={200}>From the IT knowledge base</Text>}
          />
          <Text>{answer.text}</Text>
          {answer.citations.length > 0 && (
            <div className={styles.citations}>
              {answer.citations.map((c) =>
                c.url ? (
                  <Link key={c.url} href={c.url} target="_blank" rel="noreferrer">
                    {c.title}
                  </Link>
                ) : (
                  <Text key={c.title}>{c.title}</Text>
                ),
              )}
            </div>
          )}
        </Card>
      )}
      {ticket && (
        <Card>
          <CardHeader
            header={<Text weight="semibold">Ticket {ticket.number}</Text>}
            description={<Text size={200}>{ticket.shortDescription}</Text>}
          />
          {ticket.url && (
            <Link href={ticket.url} target="_blank" rel="noreferrer">
              Open in the ticketing system
            </Link>
          )}
        </Card>
      )}
    </div>
  )
}
