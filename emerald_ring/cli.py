"""`ring` command: a thin wrapper around the package."""

import argparse
import sys
import webbrowser

import anthropic
from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import config
from . import parts as parts_lib
from . import server, stats, store
from .agent import ForgeCancelled, ForgeError, forge, forge_best, forge_free
from .brief import write_brief
from .team import forge_team
from .providers import FREE_CLOUD, PROVIDERS, make_provider
from .validate import validate_spec

console = Console()


def _load_valid(ref: str):
    try:
        path = store.resolve(ref)
        spec = store.load(path)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]✗ {escape(str(e))}[/]")
        sys.exit(1)
    errors = validate_spec(spec)
    if errors:
        console.print(f"[red]✗ {path.name} is invalid:[/]")
        for err in errors:
            console.print(f"  [red]•[/] {escape(err)}")
        sys.exit(1)
    return path, spec


def cmd_validate(args):
    path, spec = _load_valid(args.spec)
    console.print(f"[green]✓ {path.name} is valid[/] — {len(spec['parts'])} parts, "
                  f"{len(spec.get('animations', []))} animations")


def cmd_view(args):
    path, spec = _load_valid(args.spec)
    try:
        web = store.web_path(path)
    except ValueError as e:
        console.print(f"[red]✗ {escape(str(e))}[/]")
        sys.exit(1)
    console.print(f"[green]◆ Rendering[/] [bold]{spec['title']}[/] ({len(spec['parts'])} parts)")
    console.print(f"  {server.url_for(web)}")
    console.print("  [dim]Ctrl-C to stop the viewer server[/]")
    server.open_viewer(web)


class ForgeDisplay:
    """Turns forge() progress events into terminal output: a spinner plus a step log."""

    def __init__(self):
        self.status = console.status("[green]Designing the construct…[/]", spinner="dots")
        self.line = ""
        self.launched = False

    def __enter__(self):
        self.status.start()
        return self

    def __exit__(self, *exc):
        self._flush()
        self.status.stop()

    def _flush(self):
        if self.line.strip():
            console.print(f"  [dim]{escape(self.line.strip())}[/]")
        self.line = ""

    def __call__(self, kind, **d):
        if kind == "text":  # Claude's own words, printed a line at a time
            self.line += d["text"]
            while "\n" in self.line:
                done, self.line = self.line.split("\n", 1)
                done = done.replace("**", "").replace("`", "")
                if done.strip():
                    console.print(f"  [dim]{escape(done.strip())}[/]")
        elif kind == "tool_start":
            self._flush()
            label = {"web_search": "Researching",
                     "present_plan": "Drafting the plan",
                     "write_scene_spec": "Writing the scene spec",
                     "validate_scene_spec": "Validating",
                     "launch_viewer": "Opening the viewer"}.get(d["name"], d["name"])
            self.status.update(f"[green]{label}…[/]")
        elif kind == "tool_progress" and d["name"] == "write_scene_spec":
            self.status.update(f"[green]Writing the scene spec… {d['chars'] / 1000:.1f}k characters[/]")
        elif kind == "tool_done":
            mark = "[green]✓[/]" if d["ok"] else "[yellow]✗[/]"
            console.print(f"  {mark} {d['name']}: {escape(d['summary'])}")
            for err in d.get("errors", [])[:5]:
                console.print(f"      [dim]{escape(err)}[/]")
            if len(d.get("errors", [])) > 5:
                console.print(f"      [dim]… {len(d['errors']) - 5} more[/]")
            self.status.update("[green]Thinking…[/]")
        elif kind == "search":
            console.print(f"  [green]⌕[/] researched: [italic]{escape(d['query'])}[/]")
        elif kind == "probe":
            _print_probe(d)
        elif kind == "parts_found":
            console.print(f"  [green]📦[/] parts library: [italic]{escape(d['query'])}[/] → {d['count']} match(es)")
        elif kind == "reused":
            names = ", ".join(f"{u['component'].split('/')[-1]}{' (mirrored)' if u['mirror'] else ''}"
                              for u in d["used"])
            console.print(f"  [green]♻ reused[/] {escape(names)} "
                          f"[dim](+{sum(u['parts'] for u in d['used'])} parts, ≈{d['saved_tokens']:,} tokens not written)[/]")
        elif kind == "search_error":
            console.print(f"  [yellow]⌕ search problem:[/] [dim]{escape(str(d['code']))}[/]")
        elif kind == "plan_approved":
            console.print("  [green]✓ Plan approved — forging[/]")
            self.status.update("[green]Designing the full construct…[/]")
        elif kind == "plan_feedback":
            console.print(f"  [yellow]✎ Revising plan:[/] {escape(d['feedback'])}")
            self.status.update("[green]Revising the plan…[/]")
        elif kind == "inspect":
            console.print(f"  [yellow]🔍 inspector (round {d['round']}): score {d['score']}/100, sending back to fix[/]")
            for i in d["issues"]:
                console.print(f"      [dim]{escape(i[:150])}[/]")
            self.status.update("[green]Fixing what the inspector found…[/]")
        elif kind == "cooldown":
            console.print(f"  [yellow]⏳ free-tier limit: cooling down {d['seconds']}s…[/]")
            self.status.update(f"[yellow]Cooling down ({d['seconds']}s, free-tier limit)…[/]")
        elif kind == "notice":
            console.print(f"  [yellow]ℹ {escape(d['text'])}[/]")
        elif kind == "retry":
            console.print(f"  [yellow]↻ {escape(d['reason'])}[/]")
        elif kind == "saved":
            console.print(f"  [green]◆ Saved[/] {d['path'].relative_to(store.ROOT)}")
        elif kind == "done":
            self._flush()
            self.launched = d["launched"]
            u = d["usage"]
            score = f" · score {d['score']}/100" if d.get("score") is not None else ""
            cost = "" if d["cost"] is None else (" · free" if d["cost"] == 0 else f" · ~${d['cost']:.2f}")
            console.print(f"  [dim]{d['provider']} · {d['model']} · effort {d['effort']} · {u.turns} turns · {u.input + u.cache_read + u.cache_write:,} in / "
                          f"{u.output:,} out tokens{score}{cost}[/]")


    def review(self, plan):
        """Show the plan and ask: Enter approves, text requests changes, q cancels."""
        self._flush()
        self.status.stop()
        console.print(_plan_panel(plan))
        try:
            answer = console.input(
                "[bold green]Enter[/] to forge · type changes to tweak the plan · [bold]q[/] to cancel\n[green]›[/] "
            ).strip()
        except EOFError:
            answer = ""
        if answer.lower() in ("q", "quit", "cancel"):
            raise ForgeCancelled()
        self.status.start()
        return answer


def _plan_panel(plan):
    parts = [Text(plan.get("summary", ""), style="white")]
    facts = [f for f in (plan.get("dimensions"), plan.get("research")) if f]
    if facts:
        parts.append(Text("\n" + "\n".join(facts), style="dim"))

    table = Table(box=None, padding=(0, 2), show_header=True, header_style="bold green")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Component", style="bold")
    table.add_column("Shapes", style="green")
    table.add_column("Notes", style="dim")
    for i, c in enumerate(plan.get("components", []), 1):
        table.add_row(str(i), c.get("name", ""), c.get("shapes", ""), c.get("notes", ""))
    parts += [Text(""), table]

    if plan.get("animations"):
        parts.append(Text("\nAnimations", style="bold green"))
        parts += [Text(f"  ↻ {a}") for a in plan["animations"]]
    title = f"[bold green]◆ PLAN: {escape(plan.get('title', ''))}[/]  [dim]~{plan.get('estimated_parts', '?')} parts[/]"
    return Panel(Group(*parts), title=title, title_align="left", border_style="green", padding=(1, 2))


def _print_probe(d):
    ready = ", ".join(m.split("/")[-1] for m in d["healthy"]) or "none"
    skipped = ", ".join(f"{m.split('/')[-1]} ({state})" for m, state in d["skipped"])
    console.print(f"  [green]🩺 health check:[/] ready: {ready}" + (f" [dim]· skipping {skipped}[/]" if skipped else ""))


class BestDisplay:
    """Progress for best-of mode: one line per candidate event, tagged with the model."""

    QUIET = {"text", "tool_start", "tool_progress", "done", "parts_found"}

    def __init__(self, title: str = "Three free models are designing in parallel…"):
        self.status = console.status(f"[green]{title}[/]", spinner="dots")
        self.launched = False
        self.finished = False

    def __enter__(self):
        self.status.start()
        return self

    def __exit__(self, *exc):
        self.status.stop()

    def __call__(self, kind, **d):
        who = d.get("candidate", "").split("/")[-1]
        tag = f"[cyan]{who:>13}[/] " if who else ""
        if kind in self.QUIET or (self.finished and who):
            return  # quiet events, or late events from a loser that's being stopped
        if kind == "probe":
            _print_probe(d)
            return
        if kind == "start_order":
            console.print(f"  [dim]racing: {' vs '.join(m.split('/')[-1] for m in d['models'])}[/]")
            return
        if kind == "search":
            console.print(f"  {tag}[green]⌕[/] looked up: [italic]{escape(d['query'])}[/]")
            return
        if kind == "notice":
            console.print(f"  {tag}[yellow]ℹ {escape(d['text'])}[/]")
            return
        if kind == "tool_done":
            mark = "[green]✓[/]" if d["ok"] else "[yellow]✗[/]"
            console.print(f"  {tag}{mark} {d['name']}: {escape(d['summary'])}")
        elif kind == "inspect":
            console.print(f"  {tag}[yellow]🔍 score {d['score']}/100 → fixing ({len(d['issues'])} issue(s))[/]")
        elif kind == "cooldown":
            console.print(f"  {tag}[yellow]⏳ cooling down {d['seconds']}s[/]")
        elif kind == "retry":
            console.print(f"  {tag}[yellow]↻ {escape(d['reason'])}[/]")
        elif kind == "candidate_done":
            if d["ok"]:
                console.print(f"  {tag}[bold green]● finished · score {d['score']}/100[/]")
            else:
                console.print(f"  {tag}[red]● failed: {escape((d['error'] or '')[:100])}[/]")
        elif kind == "best_chosen":
            self.finished = True
            if d.get("early"):
                tail = f"good enough after {d['seconds']} s, stopped waiting for the others"
            else:
                tail = f"{d['finished']}/{d['total']} finished in {d['seconds']} s"
            console.print(f"  [bold green]★ Best: {d['model']} (score {d['score']}/100)[/] [dim]· {tail} · free[/]")
        elif kind == "saved":
            console.print(f"  [green]◆ Saved[/] {d['path'].relative_to(store.ROOT)}")


class TeamDisplay:
    """Progress for team mode: planner, builders, mirroring, assembly."""

    def __init__(self):
        self.status = console.status("[green]Planner is designing the blueprint…[/]", spinner="dots")
        self.launched = False

    def __enter__(self):
        self.status.start()
        return self

    def __exit__(self, *exc):
        self.status.stop()

    def __call__(self, kind, **d):
        who = f"[cyan]{d['who']:>14}[/] " if d.get("who") else ""
        if kind == "stage":
            self.status.update(f"[green]{escape(d['text'])}…[/]")
        elif kind == "blueprint":
            console.print(f"  [bold green]📐 Blueprint: {escape(d['title'])}[/] — {len(d['components'])} components")
            for name, mirror, detail in d["components"]:
                note = f"mirror of {mirror}" if mirror else f"~{detail} parts"
                console.print(f"      [dim]{name} ({note})[/]")
        elif kind == "blueprint_issues":
            console.print("  [yellow]📐 blueprint check found layout problems, sending back to the planner:[/]")
            for i in d["issues"][:6]:
                console.print(f"      [dim]{escape(i[:140])}[/]")
        elif kind == "built":
            console.print(f"  {who}[green]✓ built[/] {d['parts']} parts, {d['animations']} animations "
                          f"[dim]({d['model'].split('/')[-1]})[/]")
        elif kind == "mirrored":
            console.print(f"  {who}[green]⇋ mirrored[/] from {d['source']}")
        elif kind == "retry":
            console.print(f"  {who}[yellow]↻ {escape(d['reason'])}[/]")
        elif kind == "cooldown":
            console.print(f"  {who}[yellow]⏳ cooling down {d['seconds']}s[/]")
        elif kind == "team_done":
            console.print(f"  [bold green]★ Assembled: {d['parts']} parts, {d['animations']} animations · "
                          f"inspector score {d['score']}/100[/] [dim]· {d['seconds']} s · free[/]")
            for i in d["issues"]:
                console.print(f"      [dim]note: {escape(i[:140])}[/]")
        elif kind == "saved":
            console.print(f"  [green]◆ Saved[/] {d['path'].relative_to(store.ROOT)}")


def _launch(path):
    server.ensure_running()
    url = server.url_for(store.web_path(path))
    webbrowser.open(url)
    return url


def cmd_forge(args):
    description = " ".join(args.description).strip()
    if not description:
        console.print("[green]◆ GREEN LANTERN[/] [dim](unofficial fan project)[/]")
        description = console.input("[bold green]What construct should the ring forge?[/] ").strip()
        if not description:
            return
    console.print(f"[green]◆ Forging[/] [bold]{escape(description)}[/]")

    if (args.best or args.team) and args.provider == "anthropic":
        console.print("[red]✗ --best and --team use free models; they aren't available with Claude.[/]")
        sys.exit(1)

    try:
        brief = None
        if args.brief:
            try:
                with console.status("[green]Design director is writing a detailed brief…[/]", spinner="dots"):
                    brief = write_brief(
                        description,
                        provider=args.provider if args.provider not in (None, "anthropic") else "groq",
                        on_event=lambda kind, **d: console.print(
                            f"  [yellow]{'⏳ cooling down ' + str(d['seconds']) + 's' if kind == 'cooldown' else '↻ ' + escape(d.get('reason', ''))}[/]"),
                    )
                console.print(Panel(escape(brief), title="[bold green]📝 Design brief[/]", title_align="left",
                                    border_style="green", padding=(0, 1)))
            except ForgeError as e:
                # A brief is a bonus; never stop the forge because of it.
                console.print(f"  [yellow]ℹ couldn't write a brief ({escape(str(e))}); forging without one[/]")
        resolved = args.provider or config.provider()
        racing = (resolved in ("free", "groq", "gemini") and args.yes and not args.model
                  and not args.best and not args.team)
        if racing:
            display = BestDisplay(title="Racing the healthiest free models…")
            with display:
                spec = forge_free(description, on_event=display, race=True,
                                  only=None if resolved == "free" else resolved,
                                  launch_viewer=None if args.no_view else _launch,
                                  research=not args.no_research, brief=brief,
                                  reuse_parts=not args.no_reuse)
            display.launched = not args.no_view
        elif args.team:
            display = TeamDisplay()
            with display:
                spec = forge_team(description, provider=args.provider or "groq", on_event=display,
                                  launch_viewer=None if args.no_view else _launch)
            display.launched = not args.no_view
        elif args.best:
            display = BestDisplay()
            with display:
                spec = forge_best(
                    description,
                    provider=args.provider or "free",
                    on_event=display,
                    launch_viewer=None if args.no_view else _launch,
                    brief=brief,
                    research=not args.no_research,
                )
            display.launched = not args.no_view
        else:
            display = ForgeDisplay()
            with display:
                spec = forge(
                    description,
                    on_event=display,
                    review=None if args.yes else display.review,
                    launch_viewer=None if args.no_view else _launch,
                    research=not args.no_research,
                    provider=args.provider,
                    model=args.model,
                    effort=args.effort,
                    brief=brief,
                    reuse_parts=not args.no_reuse,
                )
    except (ForgeCancelled, KeyboardInterrupt):
        console.print("[yellow]Cancelled — nothing was forged.[/]")
        return
    except ForgeError as e:
        console.print(f"[red]✗ {escape(str(e))}[/]")
        sys.exit(1)
    except anthropic.AuthenticationError:
        console.print("[red]✗ Your API key was rejected. Check ANTHROPIC_API_KEY in the .env file.[/]")
        sys.exit(1)
    except anthropic.PermissionDeniedError:
        console.print("[red]✗ This API key can't use that model (or has no credit). Check console.anthropic.com.[/]")
        sys.exit(1)
    except anthropic.RateLimitError:
        console.print("[red]✗ Rate limited by the API. Wait a minute and try again.[/]")
        sys.exit(1)
    except anthropic.APIStatusError as e:
        console.print(f"[red]✗ API error {e.status_code}: {escape(str(e.message))}[/]")
        sys.exit(1)
    except anthropic.APIConnectionError:
        console.print("[red]✗ Couldn't reach the Anthropic API. Check your internet connection.[/]")
        sys.exit(1)

    path = store.CONSTRUCTS / f"{spec['id']}.json"
    console.print(f"[bold green]◆ {escape(spec['title'])}[/] — {len(spec['parts'])} parts, "
                  f"{len(spec.get('animations', []))} animations")
    if args.no_view:
        console.print(f"  View it any time: [bold]ring view {spec['id']}[/]")
        return
    owns_server = server.ensure_running()
    if not display.launched:  # Claude normally opens it; make sure it's open either way
        _launch(path)
    console.print(f"  {server.url_for(store.web_path(path))}")
    if owns_server:
        console.print("  [dim]Viewer running. Ctrl-C to stop.[/]")
        server.wait_forever()


def cmd_models(args):
    try:
        ids = make_provider(args.provider, None, "low", False).list_models()
    except ForgeError as e:
        console.print(f"[red]✗ {escape(str(e))}[/]")
        sys.exit(1)
    console.print(f"[green]◆ Models on {args.provider}[/] (use one with --model):")
    for m in ids:
        console.print(f"  {m}")


def cmd_parts(args):
    comps = parts_lib.refresh()
    query = " ".join(args.query).strip()
    shown = parts_lib.search(query, k=args.limit, components=comps) if query else \
        sorted(comps, key=lambda c: (-c.score, -len(c.parts)))[:args.limit]
    title = f"Parts matching \"{query}\"" if query else f"Parts library ({len(comps)} components)"
    t = Table(title=title, header_style="bold green", box=None, padding=(0, 2))
    t.add_column("Component", overflow="fold")
    for col in ("Parts", "Anims", "Size (m)", "From"):
        t.add_column(col, justify="right" if col in ("Parts", "Anims") else "left")
    for c in shown:
        t.add_row(c.id, str(len(c.parts)), str(len(c.animations)),
                  "×".join(f"{x:.1f}" for x in c.size), c.construct)
    console.print(t if shown else f"[dim]No parts match \"{escape(query)}\". Try a simpler word (e.g. wheel, head, wing).[/]")
    if not query:
        console.print(f"[dim]Search: ring parts wheel · ring parts dragon head · showing {len(shown)} of {len(comps)}[/]")


def cmd_stats(args):
    rows = stats.table()
    if not rows:
        console.print("[dim]No best-of runs recorded yet. Try: ring forge \"a giant mech fist\" --best[/]")
        return
    t = Table(title="Free models in --best runs", header_style="bold green", box=None, padding=(0, 2))
    for col in ("Model", "Wins", "Runs", "Finished", "Avg score", "Avg time"):
        t.add_column(col, justify="left" if col == "Model" else "right")
    for r in rows:
        t.add_row(r["model"], str(r["wins"]), str(r["runs"]), f"{r['finish_rate']:.0%}",
                  f"{r['avg_score']:.0f}" if r["avg_score"] is not None else "–",
                  f"{r['avg_seconds']:.0f} s" if r["avg_seconds"] is not None else "–")
    console.print(t)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ring", description="Green Lantern (unofficial fan project): forge hard-light constructs.")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("view", help="render a scene spec in the browser")
    p.add_argument("spec", help="e.g. samples/mech_fist, or a construct id")
    p.set_defaults(func=cmd_view)

    p = sub.add_parser("validate", help="check a scene spec against the schema")
    p.add_argument("spec")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("forge", help="describe a construct and let the ring forge it")
    p.add_argument("description", nargs="*", help='e.g. "a dragon guarding a bridge" (asks if omitted)')
    p.add_argument("--no-view", action="store_true", help="don't open the browser")
    p.add_argument("-y", "--yes", action="store_true", help="skip plan review and forge straight away")
    p.add_argument("--no-research", action="store_true", help="don't search the web for proportions")
    p.add_argument("--provider", choices=PROVIDERS,
                   help="free = the free chain (tries free Gemini/Groq models until one works); anthropic = "
                        "Claude (paid); gemini / groq / cerebras = one specific free service. Default: "
                        "Claude if you have its key, otherwise free (or RING_PROVIDER in .env)")
    p.add_argument("--model", help="model id: a Claude model (default claude-opus-5-5), or a groq/cerebras "
                                   "model from `ring models` (or RING_MODEL in .env)")
    p.add_argument("--no-reuse", action="store_true",
                   help="design everything from scratch instead of reusing parts from the library")
    p.add_argument("--brief", action="store_true",
                   help="free: a reasoning model first expands your idea into a detailed design brief")
    p.add_argument("--team", action="store_true",
                   help="free: a planner + a team of builders make it together (most detailed free mode)")
    p.add_argument("--best", action="store_true",
                   help="free: run 3 Groq models in parallel and keep the best-scoring design")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"],
                   help="how hard Claude thinks: low = fastest (default, or RING_EFFORT in .env), "
                        "higher = more detailed but slower and pricier")
    p.set_defaults(func=cmd_forge)

    p = sub.add_parser("parts", help="browse/search reusable components from your constructs")
    p.add_argument("query", nargs="*", help="e.g. wheel, dragon head, tower")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_parts)

    p = sub.add_parser("stats", help="show which free model wins --best runs most often")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("models", help="list the models a free provider offers")
    p.add_argument("--provider", choices=list(FREE_CLOUD), default="gemini")
    p.set_defaults(func=cmd_models)

    args = parser.parse_args(argv)
    if not args.cmd:  # plain `ring`: ask what to forge
        args = parser.parse_args(["forge"])
    args.func(args)


if __name__ == "__main__":
    main()
