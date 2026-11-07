# How ghmirror works, in plain language

This page is for people who need to understand ghmirror without being git experts: managers,
auditors, buyers, new team members. The technical details are in the
[guide](ghmirror-guide.pdf) and the other documents in this folder.

## The problem

Many companies keep all their source code on GitHub. The code is not the only thing there. Years
of discussion live there too: bug reports, the review of every change, who approved what and
when, release notes.

If GitHub becomes unavailable, all of that is out of reach. That can happen through an outage, a
suspended account, a compromised administrator, or someone deleting things by mistake or on
purpose. Developers may have copies of the code on their laptops, but nobody has the discussions,
the reviews or the history of decisions.

## What ghmirror does

ghmirror keeps a **complete, up-to-date second copy** of your GitHub organization in GitLab.
GitLab is a similar product that you run on your own servers. There are two copies: one in the
cloud and one on your own premises.

Think of it as a second office that receives a copy of every document, note and signature the
moment it is made, filed in exactly the same order and under the same reference numbers. Nobody
works in the second office day to day. But if the first office burns down, the team can move in
and carry on the same morning.

## What is copied

- **The code and its full history.** Every version of every file, every branch and every release
  tag, exactly as on GitHub. Each change keeps its original author and date.
- **Issues** (bug reports and tasks), with the **same numbers**. Issue #412 on GitHub is #412 on
  GitLab, so links and references in documents still make sense.
- **Pull requests**, with the **same numbers**. A pull request is a proposed change and its
  review; on GitLab it is called a merge request. Each keeps the reviewed changes, the comments,
  the line-by-line review remarks, the approvals, and who merged it and when.
- **Labels, milestones, releases and attached files.**
- **People.** Everyone gets an account on GitLab with their name, picture and keys. These
  accounts are **switched off** until they are needed, so nobody can change the copy by accident.

Some things are not copied: GitHub Discussions, project boards, the run history of GitHub Actions
(automated build jobs), and secrets such as passwords. GitHub never reveals secrets, so they must
be re-created by hand. The [support matrix](SUPPORT-MATRIX.md) has the full list.

## How the copy stays current

1. **GitHub tells ghmirror when something changes.** GitHub sends a short notification for every
   new change, comment or merge. ghmirror then fetches what changed and applies it to both
   GitLabs. In our tests, a change reached GitLab in **2 to 9 seconds**.
2. **ghmirror also checks on a schedule**, in case a notification was lost. Without notifications,
   for example when the server cannot be reached from the internet, it checks every 15 minutes.
3. **Every night it compares the copies with GitHub**, item by item, and reports any difference.
   An example report is in [samples/](samples/).

GitHub is always the **source of truth**. While GitHub is working, the GitLab copies are
read-only for people. If someone changes something on GitLab by hand, the nightly check reports
it, and the next sync puts it back to match GitHub.

## Three copies of everything

ghmirror keeps three layers, so one failure never loses everything:

| Layer | What it is | Why it exists |
|---|---|---|
| 1. Code archive | A full copy of the code on the ghmirror server | Can rebuild any GitLab copy from scratch, even if both are lost |
| 2. Record archive | The original GitHub data for every issue, pull request and comment, kept as files | Evidence of exactly what GitHub held, including old versions of things that later changed |
| 3. GitLab copies | Two GitLab servers with everything in place, ready to use | What people actually work on after a switch-over |

## It never destroys anything

A backup that copies a deletion is not a backup. ghmirror is built so that a mistake or an attack
on GitHub cannot wipe the copies:

- **Repositories, issues and pull requests are never deleted from GitLab.** A repository deleted
  on GitHub is kept on GitLab as a read-only archive. A deleted issue or pull request is closed and
  labelled. A comment or label deleted on GitHub is removed from GitLab too, so the copy matches
  GitHub, but its original text stays in the record archive, which never deletes anything.
- **The deletion guard.** If many things disappear from GitHub at once, ghmirror assumes
  something is wrong, such as a compromised account or a script gone wrong. It **stops all
  changes** to the copies and raises an alert. A person must confirm before anything continues.
- **Rewritten history is kept.** Sometimes a branch on GitHub is rewritten, which replaces its
  latest changes (a "force push"). ghmirror copies the new version, but first saves the old one in
  its code archive.
- **Work done on GitLab is saved.** If someone committed work directly on a GitLab copy, ghmirror
  saves those commits before it brings that copy back in line with GitHub.

## When GitHub is lost: the switch-over

When the team decides GitHub is gone for a while, an administrator runs **one command**
(`ghmirror promote`). In about 8 seconds for a small setup, and a few minutes for a large one, it:

1. stops copying from GitHub;
2. switches on everyone's GitLab account, linked to their company email and single sign-on;
3. puts back the rules about who may change which branch;
4. turns on GitLab's automated build jobs;
5. shows a banner on every GitLab page saying that GitLab is now the main system;
6. sets up the second GitLab to keep receiving the code from the first, so there is still a
   second copy.

Developers then change **one setting** on their computer to point at GitLab, sign in, and carry on.
Their history, their open work and the numbers of their issues and pull requests are all where
they expect them. Links to old GitHub addresses can be redirected to the GitLab pages.

The whole switch typically takes **15 to 30 minutes from the decision**. Most of that is people
reading the announcement; the software part takes minutes.

**Switching back** is also one command (`ghmirror demote`), which undoes every step exactly. That
is what makes practice runs safe. Teams should rehearse a switch-over twice a year on a test copy.

### What the second GitLab gets after a switch-over

After a switch-over, the second GitLab receives **code only**, within five minutes. It does not
receive new issues or reviews made on the new main GitLab. This is a known limit, listed in the
[support matrix](SUPPORT-MATRIX.md).

## How we know it works

Everything below was run against the real GitHub.com and real GitLab servers, not simulated:

- **A full test of every kind of item:** code, issues, pull requests merged in three different
  ways, review comments, reactions, releases and deletions. Each was checked one by one on both
  GitLabs, and the copies were identical.
- **A recorded switch-over drill:** switch to GitLab in 7.8 seconds, a developer fixes a bug and
  merges it on GitLab, switch back in 1.2 seconds. See the [recording](demo/).
- **A very large history:** the full history of the open-source `git` project (86,000 changes over
  20 years and 1,012 releases) was copied in under 5 minutes. Every change was identical on GitLab,
  down to the author, the date and the message.
- **Every problem found during testing was fixed** and is written down, with how it was fixed, in
  [ROADBLOCKS.md](../ROADBLOCKS.md).

## What it needs to run

- **One small server** for ghmirror: 2 processors, 4 GB of memory, and disk for about twice the
  size of your code ([sizing](CAPACITY.md)).
- **Two GitLab servers** that you run, one in the cloud and one on your premises. GitLab's own
  hosted service (GitLab.com) cannot be used. It does not let ghmirror keep the original authors
  and dates.
- **Read-only access to GitHub**, given once by an organization owner (two clicks during setup).
  ghmirror can read GitHub but never change it.

Setup is one command that asks a few questions and checks every connection
([install guide](INSTALL.md)).

## Honest limits

- It has been tested with a personal GitHub account and with one very large repository. It has
  not yet been tested with a full company organization of hundreds of repositories.
- Discussions, project boards and the history of automated build jobs are not copied.
- The copies are only as fresh as the last sync. If the ghmirror server itself was down when
  GitHub was lost, the copies stop at the moment the server stopped. They are still complete up to
  that moment.
- The connection that ghmirror uses for some of its work has full control over the GitLab
  servers. It must be protected like an administrator password ([security](SECURITY.md)).

## Words used here

| Word | Meaning |
|---|---|
| Repository | One project's code and its history |
| Commit | One saved change to the code, with its author, date and description |
| Branch | A line of work in a repository; the main branch holds the current version |
| Tag / release | A named, fixed version of the code, such as "version 2.1" |
| Pull request / merge request | A proposed change, its review and its approval. GitHub and GitLab use different names for the same thing |
| Force push | Replacing the latest changes on a branch instead of adding to them |
| Webhook | The notification GitHub sends when something changes |
| Promote / demote | Switch to GitLab as the main system / switch back |
