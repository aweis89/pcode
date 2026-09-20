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
    generate_completions_from_executable(bin/"pcode", "--completions")
  end

  # Homebrew's post-install relocation rewrites the universal2 (x86_64+arm64)
  # extension modules from PyPI wheels in place. The bytes come out identical,
  # but macOS then kills any Python that loads them (CODESIGNING "Invalid
  # Page"). Re-signing writes fresh files, which clears that state.
  post_install_steps do
    on_macos do
      run "/usr/bin/find", args: [
        ".", "(", "-name", "*.so", "-o", "-name", "*.dylib", ")",
        "-exec", "/usr/bin/codesign", "--force", "--sign", "-", "{}", "+"
      ], chdir: "{{libexec}}/.venv/lib"
    end
  end

  test do
    assert_match "--model", shell_output("#{bin}/pcode --help")
    assert_match "No saved sessions.",
                 shell_output("#{bin}/pcode --sessions --session-dir #{testpath}/sessions")
    assert_match "pcode", shell_output("#{bin}/pcode --theme-preview")
  end
end
