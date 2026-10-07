class Pcode < Formula
  desc "Terminal-native AI coding agent built for long-running and parallel work"
  homepage "https://github.com/cruxwell/pcode"
  url "https://github.com/cruxwell/pcode/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "f0e3e0ed720e4e7b3ff08e4deb0a6bb6707256698df3e12adc31d4440f2e0508"
  head "https://github.com/cruxwell/pcode.git", branch: "master"

  depends_on "uv" => :build
  depends_on "git-delta"
  depends_on "python@3.13"
  depends_on "shfmt"

  # The sandbox extension's shell sandbox on Linux (macOS ships sandbox-exec).
  on_linux do
    depends_on "bubblewrap"
  end

  # This upstream tap uses uv.lock rather than duplicating its dependency tree
  # as Homebrew resources. Dependency downloads require network access at build time.
  def install
    libexec.install "pyproject.toml", "uv.lock", "src", "README.md", "LICENSE"
    # The package version comes from git tags, and no .git is staged here, so
    # name it: the release itself, or 0.dev0+g<commit> for a HEAD build.
    # Scoped to pcode, so a dependency built from source keeps its own.
    ENV["SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PCODE"] = version.head? ? "0.dev0+g#{version.commit}" : version.to_s
    ENV["UV_PYTHON_DOWNLOADS"] = "never"
    ENV["UV_PROJECT_ENVIRONMENT"] = libexec/".venv"
    ENV["UV_LINK_MODE"] = "copy"
    system "uv", "sync", "--directory", libexec, "--locked", "--no-dev", "--extra", "claude",
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

  def caveats
    <<~EOS
      meridian: models need Meridian, an npm package this formula does not install.
      With Node.js installed, run:
        pcode --upgrade-meridian
      pcode then starts its own private Meridian. Details:
        https://cruxwell.github.io/pcode/providers/#local-meridian-provider
    EOS
  end

  test do
    assert_match "--model", shell_output("#{bin}/pcode --help")
    assert_match "No saved sessions.",
                 shell_output("#{bin}/pcode --sessions --session-dir #{testpath}/sessions")
    assert_match "pcode", shell_output("#{bin}/pcode --theme-preview")
  end
end
