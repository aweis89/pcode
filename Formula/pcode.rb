class Pcode < Formula
  desc "Streaming terminal for a Pydantic AI Coder agent"
  homepage "https://github.com/aweis89/pcode"
  head "https://github.com/aweis89/pcode.git", branch: "master"

  depends_on "uv" => :build
  depends_on "python@3.13"

  # This upstream tap uses uv.lock rather than duplicating its dependency tree
  # as Homebrew resources. Dependency downloads require network access at build time.
  def install
    libexec.install "pyproject.toml", "uv.lock", "src"
    ENV["UV_PYTHON_DOWNLOADS"] = "never"
    ENV["UV_PROJECT_ENVIRONMENT"] = libexec/".venv"
    ENV["UV_LINK_MODE"] = "copy"
    system "uv", "sync", "--directory", libexec, "--locked", "--no-dev",
                 "--no-editable", "--no-cache", "--python", formula_opt_bin("python@3.13")/"python3.13"
    bin.install_symlink libexec/".venv/bin/pcode"
  end

  test do
    assert_match "--model", shell_output("#{bin}/pcode --help")
    assert_match "No saved sessions.",
                 shell_output("#{bin}/pcode --sessions --session-dir #{testpath}/sessions")
    assert_match "pcode", shell_output("#{bin}/pcode --demo")
  end
end
