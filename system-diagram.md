

```mermaid
graph TD
  subgraph "GitLab"
    MR(Merge Request)
    Commit(Commit)
    Comment(Comment)
  end
  subgraph "Jira"
    Issue(Issue)
    IssueComment(Comment)
  end
  subgraph "controller"
    (Merge Request)
    Commit(Commit)
    Comment(Comment)
  end

  subgraph "Copilot Agent"
    WebhookListener(Webhook Listener)
    StateTracker(State Tracker)
    ReviewGenerator(Review Generator)
    FeedbackHandler(Feedback Handler)
  end

  MR -->|Triggers| WebhookListener
  WebhookListener --> StateTracker
  StateTracker --> ReviewGenerator
  ReviewGenerator -->|Posts Review| Comment
  Comment --> FeedbackHandler
  FeedbackHandler --> StateTracker
```