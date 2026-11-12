# Contributing

Thanks for your interest in improving the tinySA and NanoVNA front panels! Bug reports, ideas, and pull requests are all welcome.

## Reporting a bug or asking a question

Open an [Issue](../../issues). It helps to include:

- Which panel (tinySA or NanoVNA) and which instrument model and firmware version
- What you did, what you expected, and what happened instead
- Whether it also happens with the simulator (`--sim`)
- A screenshot or the error text, if there is any

## Sending a change (pull request)

1. Click **Fork** at the top right of this repository to make your own copy.
2. Make your change in your copy. Keep each pull request focused on one thing.
3. Test it (see below).
4. Click **Contribute → Open pull request** and fill in the template.

New to GitHub? GitHub's guide to [creating a pull request from a fork](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/creating-a-pull-request-from-a-fork) walks through it step by step.

## Running and testing

You need Python 3.10 or newer. From the repository root:

```
pip install -r tinysa_panel/requirements.txt
python tinysa_panel/tinysa_panel.py --sim

pip install -r nanovna_panel/requirements.txt
python nanovna_panel/vna_panel.py --sim
```

The `--sim` flag runs the panel against a built-in simulator, so you can test most changes without hardware.
If your change touches the USB serial protocol, please also test it on a real instrument and say which
model and firmware you used.

## Guidelines

- Match the style of the surrounding code.
- Don't commit build output (`dist/`, `build/`, `.exe` files). Releases are built automatically from tags.
- By contributing, you agree that your work is released under the project's [MIT License](LICENSE).
