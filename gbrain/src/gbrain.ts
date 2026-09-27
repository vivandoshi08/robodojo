// Thin wrapper over the gbrain CLI. With print=true nothing touches the
// brain: each command is echoed instead, so the whole flow can be demoed
// before GBrain or an API key is set up.

export class GBrain {
  constructor(
    readonly print = false,
    private cwd = process.cwd(),
  ) {}

  async run(args: string[], stdin?: string): Promise<string> {
    if (this.print) {
      const input = stdin === undefined ? "" : ` <<< (${stdin.length} chars of markdown)`;
      console.log(`$ gbrain ${args.map(quote).join(" ")}${input}`);
      return "";
    }
    const proc = Bun.spawn(["gbrain", ...args], {
      cwd: this.cwd,
      stdin: stdin === undefined ? "ignore" : new Blob([stdin]),
      stdout: "pipe",
      stderr: "pipe",
    });
    const [out, err, code] = await Promise.all([
      new Response(proc.stdout).text(),
      new Response(proc.stderr).text(),
      proc.exited,
    ]);
    if (code !== 0) throw new Error(`gbrain ${args.join(" ")} exited ${code}: ${err.trim()}`);
    return out;
  }

  put = (slug: string, markdown: string) => this.run(["put", slug], markdown);
  get = (slug: string) => this.run(["get", slug]);
  tag = (slug: string, tag: string) => this.run(["tag", slug, tag]);
  timelineAdd = (slug: string, date: string, text: string) => this.run(["timeline-add", slug, date, text]);
  link = (from: string, to: string, type: string) =>
    this.run(["link", from, to, "--link-type", type, "--link-source", "race-brain"]);

  /** Create the page only if it doesn't exist yet. */
  async ensurePage(slug: string, markdown: string): Promise<void> {
    if (this.print) {
      console.log(`# create ${slug} if missing`);
      return;
    }
    try {
      await this.get(slug);
    } catch {
      await this.put(slug, markdown);
    }
  }
}

const quote = (a: string) => (/^[\w./:=-]+$/.test(a) ? a : JSON.stringify(a));
