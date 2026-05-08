Feature: PR watchdog reports actionable status to the maintainer
  The maintainer wants the local watchdog to summarize what every open PR
  needs from them, with a clear evidence chain backing each verdict.

  Background:
    Given a clean state directory

  Scenario: PR with an unresolved Codex finding stays pending
    Given a poll for PR #49 with status pending and 1 open Codex thread
    When the maintainer runs the dashboard command
    Then the output shows status PENDING
    And the output mentions "0 of 1 resolved"

  Scenario: PR transitions to ready after Stage 1.5 thread sync
    Given a poll for PR #49 with status pending and 1 open Codex thread
    And a later poll for PR #49 with status ready and the thread resolved
    When the maintainer runs the dashboard command
    Then the output shows status READY
    And the output mentions "1 of 1 resolved"

  Scenario: History view collapses runs of no-change polls
    Given 3 sequential polls for PR #49 where the middle one is identical to the first
    When the maintainer runs the history command
    Then the timeline shows exactly 2 status transitions

  Scenario: Codex bot reviewing reaction holds the PR in pending
    Given a poll for PR #49 where Codex bot is still reviewing
    When the maintainer runs the dashboard command
    Then the output shows status PENDING
    And the output mentions "👀 reviewing"

  Scenario: Codex bot approval reaction lets a docs-only PR go ready
    Given a poll for PR #49 where Codex bot signaled approval and there are no findings
    When the maintainer runs the dashboard command
    Then the output shows status READY
    And the output mentions "👍 approved"

  Scenario: SWM-1103 — approve refuses when head SHA has moved since the verdict
    Given a clean state directory
    And a poll for PR #49 with status pending and 1 open Codex thread
    And a later poll for PR #49 with status ready and the thread resolved
    And the PR head has since moved to a new SHA
    When the maintainer runs the approve command
    Then the command exits non-zero
    And the approve output mentions "re-poll first"
    And no review was submitted
    And no ledger entry was written

  Scenario: SWM-1103 — approve happy path appends a ledger entry
    Given a clean state directory
    And a poll for PR #49 with status pending and 1 open Codex thread
    And a later poll for PR #49 with status ready and the thread resolved
    And the PR head still matches the verdict
    When the maintainer runs the approve command
    Then the command exits zero
    And exactly one approve review was submitted
    And exactly one ledger entry was written
