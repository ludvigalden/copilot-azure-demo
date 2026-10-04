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
import { useEffect, useState } from "react"
import { api } from "./api/client"
import type { components } from "./api/schema"

type Answer = components["schemas"]["Answer"]
type Ticket = components["schemas"]["Ticket"]
type UserProfile = components["schemas"]["UserProfile"]

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

export function App({ signIn }: { signIn?: () => void }) {
  const styles = useStyles()
  const [profile, setProfile] = useState<UserProfile | null>(null)
  const [question, setQuestion] = useState("")
  const [answer, setAnswer] = useState<Answer | null>(null)
  const [asking, setAsking] = useState(false)
  const [ticket, setTicket] = useState<Ticket | null>(null)
  const [escalating, setEscalating] = useState(false)

  useEffect(() => {
    // Sign-in is optional: the API answers this anonymous with the guest profile.
    api.GET("/me").then(({ data }) => setProfile(data ?? null))
  }, [])

  const ask = async () => {
    if (!question.trim() || asking) return
    setAsking(true)
    setAnswer(null)
    try {
      const { data } = await api.POST("/answers", { body: { question } })
      setAnswer(data ?? null)
    } finally {
      setAsking(false)
    }
  }

  const escalate = async () => {
    if (escalating) return
    setEscalating(true)
    try {
      const { data } = await api.POST("/tickets", {
        body: { shortDescription: question.slice(0, 160) || "Support request" },
      })
      setTicket(data ?? null)
    } finally {
      setEscalating(false)
    }
  }

  return (
    <div className={styles.root}>
      <div className={styles.header}>
        <Title2>IT Support Assistant</Title2>
        <div className={styles.actions}>
          {profile && <Text weight="semibold">{profile.displayName}</Text>}
          {signIn && (
            <Button appearance="secondary" onClick={signIn}>
              Sign in
            </Button>
          )}
        </div>
      </div>
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
