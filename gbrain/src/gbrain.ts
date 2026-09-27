// Thin wrapper over the gbrain CLI. With print=true nothing touches the
// brain: commands are echoed instead, for trying things before GBrain is set up.

export class GBrain {
  constructor(readonly print = false) {}

  async run(args: string[], stdin?: string): Promise<string> {
    if (this.print) {
      const input = stdin === undefined ? "" : ` <<< (${stdin.length} chars)`;
      console.log(`$ gbrain ${args.join(" ")}${input}`);
      return "";
    }
    const proc = Bun.spawn(["gbrain", ...args], {
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
}
