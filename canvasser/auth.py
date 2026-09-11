"""SSO login and session liveness.

The liveness check rests on a fact mapped during reconnaissance: an
*unauthenticated* request to the Canvas root is redirected away, while an
authenticated one stays put and renders the dashboard. "Which host did we end
up on" is therefore a reliable, markup-independent signal -- far more durable
than probing for some element Canvas may restyle next term.

**It is an allowlist: authenticated iff we are still on the configured Canvas
host.** It used to be a denylist of the two hosts UF bounces to, which meant
every new institution had to enumerate every host it might land on -- and
recon 2026-09-09 showed those hosts are not even the same *kind* of thing.
UF's unauthenticated root lands on `elearning.ufl.edu`, a WordPress help site;
Temple's lands on `fim.temple.edu`, the IdP itself. One allowlist covers both
with no per-institution configuration, and it is the simpler code.

## Signing in: read the page, do not follow a script

`IDPS` holds one row per identity-provider family -- Shibboleth (UF, Temple)
and Microsoft Entra ID (UCF) -- and `detect_idp` decides which is on screen
from the page's own controls, refusing when two match or none does.

**The sequence is not fixed, and assuming one is how this went wrong.** The
same Entra method picker is a *primary credential* when the account is
remembered and a *second factor* after a password, so nothing here labels steps
by position. What is asked for depends on the tenant, the account, and the
profile's own history.

`log_in` is therefore a **recogniser loop**: a set of page states, each with a
detector and an answer, dispatched against whatever is on screen until we are
authenticated or nothing matches.

    a username                  -> type it                (absent if known)
    which credential to use     -> password, or the app with --passwordless
    a password                  -> type it       (absent if none is wanted)
    which verification method   -> choose by authMethodId
    an authenticator code       -> prompt the human; never stored
    a Duo challenge             -> hand to the approver

**No state knows its index**, so three factors, one, or none is one code path,
and a provider that inserts a screen tomorrow needs a row in `RECOGNISERS`
rather than a re-plumbing. **Whether a password is asked for at all is the
provider's decision**, not the caller's, so the ordinary path proceeds happily
when none is offered. The derivation is in
`workbook/designs/auth-factors.md` in the project's own notes.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import NoReturn

from playwright.sync_api import (Error as PlaywrightError, Page,
                                 TimeoutError as PlaywrightTimeout)

from .browser import save_debug_snapshot
from .config import Config, canvas_host
from .credentials import PASSWORD_VAR, USERNAME_VAR
from .duo import Approver, PushApprover, complete_duo, on_duo_page
from .progress import code_box, glyphs_for, print_box

#: UF's unauthenticated landing site -- a WordPress help site, NOT the IdP.
#: No longer part of any decision here: liveness is an allowlist, and the
#: credential check is structural. Kept because "never point tooling at
#: elearning.ufl.edu" is a standing rule and the name is what makes it
#: findable. Confirmed still true 2026-09-09: UF's own `/login` redirects here.
ELEARNING_HOST = "elearning.ufl.edu"


@dataclass(frozen=True)
class IdP:
    """One identity-provider family's credential form.

    **A table, because the set grows** -- the same reasoning as `TITLE_FIELDS`
    and `INFO_COMPARISONS`. Until 2026-09-11 these three selectors were bare
    module constants, which was honest while every institution ran Shibboleth
    and became wrong the moment one did not.
    """

    #: What to call it in an error message a person has to act on.
    name: str
    username_field: str
    password_field: str
    submit: str
    #: Whether the username is submitted **on its own**, before the password
    #: box becomes usable. Not cosmetic: it decides whether a username box and
    #: a password box are one state or two.
    two_screen: bool
    #: Every selector that identifies this family, not just its username box.
    #: **An IdP that remembers the account never shows a username field at
    #: all** -- measured at UCF 2026-09-11, where a profile warmed by earlier
    #: runs went straight to the passkey challenge. Identifying a family only
    #: by the box you type a name into makes a remembered account
    #: unrecognisable.
    markers: tuple[str, ...] = ()


#: Every IdP family this build can drive. **Order is not significance** --
#: detection matches on the page's own controls and refuses when more than one
#: row could apply, so adding a row cannot silently re-route an existing
#: institution.
IDPS: tuple[IdP, ...] = (
    # UF and Temple. `j_username`/`j_password` are JAAS names, not a Canvas
    # convention -- which is exactly why they could never have been a default.
    IdP(name="Shibboleth",
        username_field="input[name='j_username']",
        password_field="input[name='j_password']",
        submit="button[name='_eventId_proceed']",
        two_screen=False,
        markers=("input[name='j_username']", "input[name='j_password']")),
    # UCF (measured 2026-09-11). Microsoft Entra ID. **`#idSIButton9` is
    # deliberately the submit for both screens** -- Microsoft reuses that id,
    # labelling it "Next" on the username screen and "Sign in" on the password
    # screen, so one selector genuinely serves both steps.
    IdP(name="Microsoft Entra ID",
        username_field="input[name='loginfmt']",
        password_field="input[name='passwd']",
        submit="#idSIButton9",
        two_screen=True,
        # The picker link and the credential tiles are what a remembered
        # account shows instead of a username box.
        markers=("input[name='loginfmt']", "input[name='passwd']",
                 "#idA_PWD_SwitchToCredPicker",
                 "[role=button][data-test-cred-id]")),
)

#: The Shibboleth row's selectors, kept under their original names because they
#: are what the error messages and the existing checks refer to.
SHIBBOLETH = IDPS[0]
USERNAME_FIELD = SHIBBOLETH.username_field
PASSWORD_FIELD = SHIBBOLETH.password_field
SUBMIT_BUTTON = SHIBBOLETH.submit


class LoginError(RuntimeError):
    """Raised when authentication cannot be completed."""


def _log(message: str) -> None:
    print(f"  {message}", file=sys.stderr, flush=True)


def _settle(page: Page, timeout_ms: int = 15_000) -> None:
    """Let the page finish loading, without demanding network silence.

    `networkidle` is the wrong tool anywhere Duo may be involved: Duo holds a
    long-poll connection open while it waits for the user to tap approve, so the
    network never goes idle and the wait times out *mid-login* -- killing the
    flow during the exact window the human is being asked to act. Learned the
    hard way on the first live run.

    A timeout here is not fatal; the caller's own URL/selector checks decide
    whether we actually got where we meant to go.
    """
    try:
        page.wait_for_load_state("load", timeout=timeout_ms)
    except PlaywrightTimeout:
        pass


def quiesce(page: Page, timeout_ms: int = 15_000) -> None:
    """Wait for network idle where it genuinely helps, but never block on it.

    The IdP page does a client-side navigation shortly after load, so touching
    the DOM too early destroys the evaluation context. Waiting for idle avoids
    that -- but idle is not guaranteed to arrive, so a timeout is tolerated
    rather than raised. See `_settle` for why this matters on Duo.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeout:
        pass


def is_logged_in(page: Page, config: Config) -> bool:
    """Return whether the persistent session still has us authenticated."""
    page.goto(config.base_url, wait_until="domcontentloaded")
    quiesce(page)
    return _looks_authenticated(page.url, config)


def _looks_authenticated(url: str, config: Config) -> bool:
    """Whether this URL means we are logged in to THIS Canvas.

    **An allowlist of one host**, not a denylist of the places we might have
    been sent. Compared on the parsed hostname rather than by substring: a
    substring test would call `https://evil.example/?ufl.instructure.com`
    authenticated, and would also match a host that merely ends with the right
    letters.
    """
    return canvas_host(url) == config.host


#: Is any of these password boxes one a PERSON could actually type into?
#:
#: **Not `element.isVisible()`, and that distinction is measured rather than
#: fussy.** UCF's Entra screen-one password box sits at 10x13px in a corner
#: with `opacity: 0` and `aria-hidden="true"` -- and Playwright's `is_visible()`
#: calls it visible, because it asks only about a bounding box, `display` and
#: `visibility`. A predicate built on that would read "the password form is
#: still up" on a screen where no password field is on show at all.
#:
#: Opacity is checked up the whole ancestor chain: a fully opaque input inside
#: a zeroed-out parent is invisible, and that is exactly how Entra parks the
#: view it is not currently showing.
OPERABLE_PASSWORD = """
(selectors) => {
  const operable = (sel) => {
    const e = document.querySelector(sel);
    if (!e || e.disabled) return false;
    if (e.getAttribute('aria-hidden') === 'true') return false;
    const r = e.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    for (let n = e; n; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      if (parseFloat(cs.opacity) === 0) return false;
    }
    return true;
  };
  return selectors.some(operable);
}
"""

#: True once no credential form the user could still be looking at remains.
#: That is the STRUCTURAL signal that primary authentication was accepted: an
#: IdP re-presents its own form when it rejects a password and moves on when it
#: does not.
#:
#: **One question, one implementation.** The previous version asked whether the
#: password input was absent from the DOM, which is true of Shibboleth (it
#: removes the field) and false of Entra (which keeps it, hidden, the whole
#: time). Two predicates for one question is the shape that has cost this
#: project three separate bugs, so the test is instead "is it *operable*" --
#: correct for both, since an absent field is not operable either.
NO_OPERABLE_PASSWORD = f"""
(selectors) => !({OPERABLE_PASSWORD})(selectors)
"""


def detect_idp(page: Page, timeout_ms: int = 30_000) -> IdP:
    """Which IdP family's form is on screen.

    **Decided by the page's own controls, never by the host**, and it
    **refuses when unsure** rather than picking. Submitting a password to a
    form we have misidentified is the one mistake in this file worth refusing
    outright -- the same rule as the ambiguous Save button on the write path.
    """
    any_marker = ", ".join(m for idp in IDPS for m in idp.markers)
    try:
        # **`state="attached"`, and the default would be a bug.** A
        # comma-separated CSS list resolves to the FIRST match in DOM order,
        # and `wait_for_selector` waits for *visibility* by default -- so on
        # Entra's passkey page, where `loginfmt` is present but hidden and the
        # picker link is present and visible, the wait blocks on the hidden
        # one and times out. Measured 2026-09-11 against the captured page.
        # Identifying a family is a question about PRESENCE, not visibility.
        page.wait_for_selector(any_marker, timeout=timeout_ms, state="attached")
    except PlaywrightTimeout as exc:
        snapshot = save_debug_snapshot(page, "login-form-missing")
        raise LoginError(
            f"The SSO login form never appeared (url={page.url}). Looked for "
            f"{', '.join(idp.name for idp in IDPS)}. If this institution uses a "
            f"different identity provider, it needs a new row in "
            f"`auth.IDPS`. Snapshot: {snapshot}"
        ) from exc

    matched = [idp for idp in IDPS
               if any(page.locator(m).count() for m in idp.markers)]
    if len(matched) != 1:
        snapshot = save_debug_snapshot(page, "login-form-ambiguous")
        named = ", ".join(idp.name for idp in matched) or "none"
        raise LoginError(
            f"Could not tell which identity provider this is (url={page.url}); "
            f"matched: {named}. Refusing rather than guessing -- credentials "
            f"must not be typed into a form we have misidentified. "
            f"Snapshot: {snapshot}"
        )
    return matched[0]



def _submit_single_screen(page: Page, config: Config, idp: IdP) -> None:
    """Both fields and one click -- Shibboleth, and proven live at UF and Temple."""
    page.fill(idp.username_field, config.username)
    page.fill(idp.password_field, config.password)
    page.click(idp.submit)
    page.wait_for_load_state("domcontentloaded")


#: Tell the IdP, truthfully, that there is no passkey here.
#:
#: **Without this the login hangs rather than failing.** Measured at UCF
#: 2026-09-11: the tenant's default method is a passkey, and headless Chromium
#: has no authenticator at all -- so `navigator.credentials.get()` never
#: settles. A pending promise is a state no real browser rests in, so the page
#: sits on its progress bar forever and never offers the alternatives it would
#: offer a person who declined.
#:
#: Rejecting with `NotAllowedError` is exactly what a cancelled or unavailable
#: authenticator produces, and it is **an honest statement of capability**: a
#: passkey is hardware-bound, and this process has no hardware to hold one.
#: Only WebAuthn requests are answered this way; anything else passes through.
NO_PASSKEY = """
if (navigator.credentials && navigator.credentials.get) {
  const real = navigator.credentials.get.bind(navigator.credentials);
  navigator.credentials.get = (opts) => (opts && opts.publicKey)
    ? Promise.reject(new DOMException(
        'No authenticator is available to this client.', 'NotAllowedError'))
    : real(opts);
}
"""

#: "Sign in another way" -- Entra's credential picker. Measured, not guessed.
ENTRA_CRED_PICKER = "#idA_PWD_SwitchToCredPicker"

#: The password tile in that picker. `data-test-cred-id` is Entra's credential
#: *type*, and `1` is password; the tile is a `role=button` div, not a link.
#: **The label is cross-checked before it is clicked** -- "the id still exists"
#: and "the id still means what it meant" are different claims, and the cost of
#: clicking the wrong tile here is a push notification at a real person's phone.
ENTRA_PASSWORD_TILE = '[role=button][data-test-cred-id="1"]'
ENTRA_PASSWORD_WORDS = ("password",)

#: The same picker's **passwordless** tile: the authenticator app as the
#: PRIMARY credential, not as a second factor. Measured 2026-09-11 alongside
#: the password tile (`7` is the passkey, `1` the password, `2` this).
#:
#: **This is a different thing from `ENTRA_PUSH`**, which appears later and
#: only after a password. Same app, same tap, different role in the flow --
#: and choosing it means there is no password to store at all.
ENTRA_APP_TILE = '[role=button][data-test-cred-id="2"]'
ENTRA_APP_WORDS = ("authenticator", "approve")

#: Entra's passwordless challenge. Measured at UCF 2026-09-11: the flow leaves
#: `login.microsoftonline.com` for `login.microsoft.com/<tenant>/fido/get`, and
#: the page's own `$Config` carries `sFidoChallenge`. **Both are checked** --
#: the URL because it is settled and cheap, the config key because a route can
#: be renamed while the capability cannot.
FIDO_MARKERS = """
() => ({
  url: /\\/fido\\//.test(location.pathname),
  challenge: !!(window.$Config && window.$Config.sFidoChallenge),
})
"""


def _entra_stall_reason(page: Page, idp: IdP) -> str:
    """Why no password box appeared -- in terms the reader can act on.

    Two causes need opposite fixes, and telling them apart is the whole point:

    * **Still on the username screen** -- the username was refused. Go and fix
      the credential setting.
    * **Moved on to something that is not a password** -- the username was
      *accepted*, and the account is being asked for a different factor. The
      credential setting is correct and editing it makes things worse.

    A passkey is the case that matters, and it is not a selector problem: a
    FIDO2 credential lives in hardware, so there is nothing this tool could
    type even in principle. Saying so plainly beats a message that implies a
    fix exists.
    """
    try:
        if page.evaluate(OPERABLE_PASSWORD, [idp.username_field]):
            return (f"The sign-in form is still asking for a username, so it "
                    f"was not accepted -- check {USERNAME_VAR} in the secrets "
                    f"file, and whether this provider wants a full address "
                    f"rather than a bare name.")
    except PlaywrightError:
        pass

    try:
        fido = page.evaluate(FIDO_MARKERS)
    except PlaywrightError:
        fido = {}
    if fido.get("url") or fido.get("challenge"):
        return (
            "The username WAS accepted -- this provider is asking for a "
            "passkey or security key (FIDO2: face, fingerprint, PIN or "
            "hardware key) instead of a password. That is an account or tenant "
            "policy, not a configuration error here, and it cannot be "
            "satisfied by a stored password: the credential lives in hardware. "
            f"Leave {USERNAME_VAR} alone. Either enable password sign-in for "
            "this account, or choose another sign-in method if the provider "
            "offers one.")

    offered = _entra_factor_offered(page)
    if offered:
        # Measured 2026-09-11: a remembered account is taken straight to
        # "Verify your identity", whose tiles are verification METHODS and
        # include no password at all. Naming the flag is the actionable part.
        return (
            f"The username was accepted and this provider went straight to "
            f"choosing a verification method ({', '.join(offered)}) -- it is "
            f"not offering a password at all. Use --passwordless to sign in "
            f"with the authenticator app instead.")

    return ("The username appears to have been accepted, but the next screen "
            "was not a password box -- this provider may be asking for another "
            f"factor. Check the snapshot before changing {USERNAME_VAR}.")




def _choose_password_method(page: Page) -> bool:
    """Open the picker and select the password. Kept as its own name because
    that is what the password path asks for; the work is shared below."""
    return _choose_primary_method(page, ENTRA_PASSWORD_TILE,
                                  ENTRA_PASSWORD_WORDS)


def _choose_primary_method(page: Page, tile_selector: str,
                           expect_words: tuple[str, ...]) -> bool:
    """Open Entra's credential picker and select one PRIMARY credential.

    Returns whether it got there. **Selects the password tile and nothing
    else**: the other tiles include "Approve a request on my Microsoft
    Authenticator app", and clicking that sends a notification to a real
    person's phone that nobody is waiting for. This project has done that
    once already, by resolving the wrong institution; it is not a mistake to
    make twice by fumbling a picker.
    """
    picker = page.locator(ENTRA_CRED_PICKER)
    try:
        # **Wait for it rather than sampling once.** The link appears only
        # after the passkey attempt has been declined and the page has
        # re-rendered, so an immediate `count()` races that and reports "no
        # picker offered" for a page that is about to offer one. A state, not
        # a duration -- the rule this project keeps re-earning.
        picker.first.wait_for(state="visible", timeout=20_000)
        picker.first.click()
    except (PlaywrightTimeout, PlaywrightError):
        return False

    tile = page.locator(tile_selector)
    try:
        tile.wait_for(state="visible", timeout=15_000)
    except (PlaywrightTimeout, PlaywrightError):
        return False

    # More than one match means the credential id no longer identifies one
    # control, so refuse rather than pick -- the ambiguous Save button rule.
    if tile.count() != 1:
        return False

    # The id says *which* credential type; the label says what the user is
    # being offered. Both must agree before we click something that could be
    # a second factor rather than a password.
    label = (tile.first.get_attribute("aria-label") or "").lower()
    if label and not any(w in label for w in expect_words):
        _log(f"Refusing the credential tile: its id and its label {label!r} "
             f"disagree about what it is.")
        return False

    tile.first.click()
    return True


def _raise_entra_stall(page: Page, idp: IdP) -> None:
    """Report why no password box appeared, and stop."""
    complaint = _idp_complaint(page)
    snapshot = save_debug_snapshot(page, "login-username-stuck")
    said = f" It says: {complaint!r}." if complaint else ""
    raise LoginError(
        f"{idp.name} never presented a password box after the username was "
        f"submitted (url={page.url}).{said} {_entra_stall_reason(page, idp)} "
        f"Nothing was retried. Snapshot: {snapshot}"
    )


#: Words an IdP uses when it turns credentials down. **These no longer decide
#: anything** -- they are used to quote the page back to the user. Deciding by
#: wording meant only UF's phrasing was ever recognised.
#: The last four are Entra-shaped: Microsoft answers an unknown username with
#: "We couldn't find an account with that username", which contains none of the
#: words a Shibboleth IdP uses. **Widening this is safe precisely because it
#: decides nothing** -- it only chooses which of the page's own sentences is
#: worth quoting back.
REJECTION_WORDS = ("incorrect", "invalid", "failed", "try again", "locked",
                   "unsuccessful", "not recognized", "again",
                   "couldn't find", "could not find", "doesn't exist",
                   "does not exist")


def _idp_complaint(page: Page) -> str:
    """Whatever the IdP says about the rejection, if it says anything.

    Best-effort and never load-bearing: an IdP that explains itself gets
    quoted, and one that does not still produces a clear error.
    """
    try:
        lines = page.evaluate(
            """() => Array.from(document.querySelectorAll(
                   '.alert, .form-error, [role=alert], .output--error, p, span'))
                 .map(e => (e.innerText || '').trim())
                 .filter(t => t && t.length < 200)""")
    except PlaywrightError:
        return ""
    for line in lines or []:
        low = line.lower()
        if any(word in low for word in REJECTION_WORDS):
            return line
    return ""


def _check_for_credential_rejection(
    page: Page, config: Config, timeout_ms: int = 20_000,
    idp: IdP | None = None,
) -> None:
    """Fail fast and clearly on a bad password, rather than timing out later.

    Worth being decisive: repeatedly submitting a wrong password is how
    accounts get locked out, which is far worse than an early error.

    **The signal is structural, not textual** -- the credential form either
    survives the submission or it does not. That works at any Shibboleth IdP,
    including one whose wording nobody here has ever read, which the previous
    version could not: it matched phrases seen on UF's page, so a wrong
    password anywhere else fell through to a vague "we are not authenticated"
    twenty seconds later. The page's own words are still *quoted* when it
    offers any (`_idp_complaint`); they simply no longer decide.

    **Waiting for a state, never a duration.** Checking "is the form still
    there" after a fixed settle races the IdP's navigation -- the same race
    that crashed a live push from the write path's post-save wait. So this
    waits for the form to *go*, and only a timeout means it stayed.
    """
    if _looks_authenticated(page.url, config):
        return  # already through; nothing was rejected

    # With no family named, ask about every one we know. That is the honest
    # question at this point -- "is ANY credential form still up" -- and it
    # keeps the check usable by callers that did not do the submitting.
    selectors = ([idp.password_field] if idp
                 else [i.password_field for i in IDPS])
    try:
        page.wait_for_function(NO_OPERABLE_PASSWORD, arg=selectors,
                               timeout=timeout_ms)
        return
    except PlaywrightTimeout:
        pass
    except PlaywrightError:
        # The context was destroyed under us, which means the page navigated --
        # and navigating away IS the success signal. Not an error. Same
        # reasoning as `writer._settle_after_save`, which guards its evaluate
        # for exactly this.
        return

    complaint = _idp_complaint(page)
    snapshot = save_debug_snapshot(page, "login-rejected")
    said = f" It says: {complaint!r}." if complaint else ""
    raise LoginError(
        f"The sign-in form at {canvas_host(page.url)} was still on screen "
        f"{timeout_ms // 1000}s after the credentials were submitted, so they "
        f"were not accepted.{said} Check {USERNAME_VAR} / {PASSWORD_VAR} in "
        f"the secrets file. Nothing was retried -- repeated attempts lock "
        f"accounts. Snapshot: {snapshot}"
    )


def log_in(page: Page, config: Config, approver: Approver | None = None) -> None:
    """Perform a full SSO login, whatever the provider decides to ask for.

    **A recogniser loop, not a sequence** (user's design, 2026-09-11). The
    question at every step is *"is anything being asked, and can we answer
    it?"* -- never *"what comes third?"*. See `workbook/designs/auth-factors.md`.

    The evidence that ordering is the wrong concept: **Entra's method picker is
    a primary credential on one path and a second factor on another.** It
    appears after a password, where it is unambiguously a second factor, and
    *instead of* a password when the account is remembered, where the same tap
    IS the credential. A model that assigns positions mislabels one of them --
    which is exactly the error that told the user their sign-in needed a
    password they never see. The sequence is not a property of the institution
    either: the same UCF account, hours apart, was asked for a username then a
    passkey then a password, and later for nothing but a method. What changed
    was state this tool created by keeping a profile.

    So each state is recognised by what is on screen and handled on its own.
    Three factors, one, or none is one code path.
    """
    approver = approver or PushApprover()

    _log("Session expired or absent -- authenticating.")
    # Declare "no passkey" before the IdP can ask. This has to be installed
    # before any page script runs, and an IdP whose default method is a passkey
    # will otherwise leave us on a progress bar forever -- see `NO_PASSKEY`.
    page.add_init_script(NO_PASSKEY)
    page.goto(config.sso_url, wait_until="domcontentloaded")
    quiesce(page)

    # A still-valid IdP session can carry us straight through without a form.
    if _looks_authenticated(page.url, config):
        _log("IdP session still valid; no credentials needed.")
        return

    # **Which family, decided once, from the page's own controls.** This is the
    # same idea as the loop one level up: read what is there rather than trust
    # configuration. It refuses when two match or none does.
    idp = detect_idp(page)
    _log(f"Identity provider: {idp.name}.")

    state = LoginState(page=page, config=config, approver=approver, idp=idp)
    _drive_login(state)

    if not state.answered_a_challenge:
        # **Not necessarily a remembered device.** At UF, no Duo page means Duo
        # remembered this browser; at Temple it means the IdP asked for no
        # second factor at all (observed on the first live login, 2026-09-09).
        # Reporting the UF reading everywhere would tell a Temple user their
        # device is remembered when nothing is remembering anything.
        _log("No second factor presented (remembered device, or none required "
             "by this institution).")
    _log(f"Authenticated. Landed on {page.url}")


def _drive_login(state: LoginState) -> None:
    """Answer whatever the provider asks, until it stops asking.

    Returns only when authenticated; every other exit raises, so a caller can
    never mistake "we gave up" for "we are in".
    """
    while True:
        if _looks_authenticated(state.page.url, state.config):
            return

        found = _match(state)
        if found is not None:
            if len(state.fired) >= MAX_LOGIN_STEPS:
                _refuse(state, "login-too-many-steps",
                        f"stopped after {MAX_LOGIN_STEPS} authentication "
                        f"steps without getting through")
            state.fired.append(found.name)
            _log(f"The provider is asking for {found.name}.")
            if found.challenges_human:
                state.answered_a_challenge = True
            found.handle(state)
            _settle(state.page)
            continue

        # Nothing we recognise is on screen. Either the flow is finishing (a
        # redirect chain, or a challenge in flight on someone's phone) or it has
        # stopped somewhere this build does not understand. Both are answered by
        # watching -- for authentication OR for a state we do know.
        if _wait_for_progress(state):
            continue
        _refuse(state, "login-incomplete",
                "stopped at a state this build does not recognise")


def _match(state: LoginState) -> Recogniser | None:
    """The first recogniser that both matches and has not already fired.

    **Order is a tie-break, not a sequence.** Every detector asks about the page
    in front of it, so in a healthy flow at most one matches; the ordering only
    decides what happens if two ever did. `idp_check` asserts single-match on
    every fixture, which is evidence rather than a promise.
    """
    for recogniser in RECOGNISERS:
        # **Once each.** Every state here is a one-shot in every flow measured,
        # and this is what stops a provider re-presenting a challenge from
        # getting it answered twice -- each push rings a real phone.
        if recogniser.name in state.fired:
            continue
        try:
            if recogniser.detect(state):
                return recogniser
        except PlaywrightError:
            # A page mid-navigation destroys the evaluation context. That means
            # the question cannot be answered *right now*, not that the state is
            # absent; the caller polls again.
            continue
    return None


def _wait_for_progress(state: LoginState) -> bool:
    """Watch for authentication, or for a state we know how to answer.

    Returns whether something happened worth looping on.

    **Polls rather than sampling once.** After a second factor the browser is
    still walking a redirect chain (Duo -> IdP -> SAML POST -> Canvas), and a
    single check runs mid-chain and reports failure for a login that is merely
    still in flight -- which once produced a "not authenticated" error for a
    session sitting on the Canvas dashboard.

    **It watches for new STATES too**, which the old fixed wait could not. A
    provider that inserts a screen after the factor -- Entra's "Stay signed
    in?", never yet seen here -- is then answered if a recogniser exists, and
    named rather than left a mystery if one does not.
    """
    # Long enough for a person to find their phone when one is waiting on them;
    # the shorter one is right for a redirect chain and absurd for a human.
    deadline_ms = (state.human_patience_ms if state.awaiting_human
                   else state.patience_ms)
    waited = 0
    while waited < deadline_ms:
        if _looks_authenticated(state.page.url, state.config):
            return True
        if _match(state) is not None:
            return True
        try:
            state.page.wait_for_timeout(1_000)
        except PlaywrightError:
            return False
        waited += 1_000
    return False


def _refuse(state: LoginState, label: str, why: str) -> NoReturn:
    """Stop, and say what the provider was offering when we stopped.

    **Name what WAS offered.** Entra's tiles carry every method registered on
    the account, so a refusal here can always be specific -- and a specific
    refusal is the difference between "it did not work" and "this account only
    has a security key, which needs hardware this process does not have".
    """
    page = state.page
    # **A rejected USERNAME is reported as itself, not as a bad password.** The
    # two need opposite fixes and send the reader to different lines of the
    # secrets file, so the specific diagnosis wins over the generic refusal
    # wherever it applies.
    if _looks_like_a_rejected_username(state):
        _raise_entra_stall(page, state.idp)

    offered = _entra_factor_offered(page)
    handled = ", ".join(state.fired) if state.fired else "nothing"
    detail = ""
    if offered:
        detail = (f" The provider is offering: {', '.join(offered)} -- none of "
                  f"which this build can answer.")
    # The page's own words, which need no selector and work for states this
    # code has never met.
    _report_page_text(page)
    snapshot = save_debug_snapshot(page, label)
    raise LoginError(
        f"Login {why} (url={page.url}). Answered so far: {handled}.{detail} "
        f"Nothing was retried. Snapshot: {snapshot}"
    )


# --- the states, and how each is answered -----------------------------------

#: Most states one sign-in may pass through before this stops and reports.
#:
#: **A bound, not a budget.** Every recogniser fires at most once, so a healthy
#: login cannot reach it; it exists so a provider that cycles stops rather than
#: spins.
MAX_LOGIN_STEPS = 12


@dataclass
class LoginState:
    """What the loop knows while driving one sign-in."""

    page: Page
    config: Config
    approver: Approver
    idp: IdP
    #: Which states have been answered, in order. Doubles as the "once each"
    #: guard and as what a refusal reports having done.
    fired: list[str] = field(default_factory=list)
    #: True once a challenge is in flight on a person's phone, which is the only
    #: reason to wait minutes rather than seconds.
    awaiting_human: bool = False
    #: Whether any second factor was answered at all -- so the run can say
    #: "none was presented" without claiming to know why.
    answered_a_challenge: bool = False
    #: How long to watch for something to happen when nothing is on screen we
    #: recognise. **Named rather than buried**: these were two magic numbers in
    #: the old sequence, and one of them is the difference between "a redirect
    #: chain is still walking" and "a person has to find their phone".
    patience_ms: int = 30_000
    human_patience_ms: int = 150_000


@dataclass(frozen=True)
class Recogniser:
    """One page state, and what to do about it.

    **Named for what the PROVIDER is asking**, not for a step number. That is
    the whole point of the design: `name` appears in the log and in refusals,
    and reads correctly whichever position the state turns up in.
    """

    name: str
    detect: Callable[[LoginState], bool]
    handle: Callable[[LoginState], None]
    #: Whether answering this asks something of a person. Drives both the long
    #: wait and the "no second factor presented" line.
    challenges_human: bool = False


def _operable(state: LoginState, *selectors: str) -> bool:
    """Whether any of these is a control a person could actually use.

    **Never `is_visible()`**, which ignores opacity: Entra's screen-one password
    box is `opacity: 0`, `aria-hidden` and 10x13px in a corner, and Playwright
    calls it visible while `fill` succeeds on it. Typing a password into a box
    nobody can see raises nothing at all.

    This is what makes the detectors trustworthy enough to double as the wait.
    The old code waited for an operable password box and then typed; here the
    handler is only reachable when the box is *already* operable, so the
    guarantee is structural rather than sequential.
    """
    return bool(state.page.evaluate(OPERABLE_PASSWORD, list(selectors)))


def _present(state: LoginState, selector: str) -> bool:
    """Whether the selector matches anything at all -- presence, not visibility."""
    try:
        return state.page.locator(selector).count() > 0
    except PlaywrightError:
        return False


def _sees_duo(state: LoginState) -> bool:
    return on_duo_page(state.page)


def _answer_duo(state: LoginState) -> None:
    _log(f"Using '{state.approver.name}' approval.")
    # Snapshot the Duo page while we are actually on it. Its DOM is otherwise
    # unobservable during development (reaching it needs real credentials), and
    # this is the evidence any future selector repair depends on.
    _log(f"Duo page captured: {save_debug_snapshot(state.page, 'duo-live')}")
    complete_duo(state.page, state.approver)


def _sees_single_screen_form(state: LoginState) -> bool:
    """Shibboleth: both fields on one form, answered in one go.

    **`two_screen` is what keeps this and "a password" from both matching.**
    A Shibboleth form holds an operable password box too, so without the gate
    two states would claim the same page -- and the loop would be relying on
    its own ordering to resolve something that should not be ambiguous at all.
    """
    return (not state.idp.two_screen
            and _operable(state, state.idp.password_field))


def _answer_single_screen_form(state: LoginState) -> None:
    _submit_single_screen(state.page, state.config, state.idp)
    _settle(state.page)
    _check_for_credential_rejection(state.page, state.config, idp=state.idp)


def _sees_username_box(state: LoginState) -> bool:
    return state.idp.two_screen and _operable(state, state.idp.username_field)


def _answer_username_box(state: LoginState) -> None:
    state.page.fill(state.idp.username_field, state.config.username)
    state.page.click(state.idp.submit)
    state.page.wait_for_load_state("domcontentloaded")


def _sees_password_box(state: LoginState) -> bool:
    # **Under `--passwordless` a password box is not a state we answer.** The
    # app is the credential; typing a stored password would silently demote it
    # to a second factor, which is the opposite of what was asked for.
    if state.config.passwordless:
        return False
    # A password ON ITS OWN is the two-screen shape. A single-screen form is
    # answered whole, by the recogniser above.
    if not state.idp.two_screen:
        return False
    return _operable(state, state.idp.password_field)


def _answer_password_box(state: LoginState) -> None:
    state.page.fill(state.idp.password_field, state.config.password)
    state.page.click(state.idp.submit)
    state.page.wait_for_load_state("domcontentloaded")
    _settle(state.page)
    # **Fail fast on a rejection, next to the submission that could be
    # rejected.** Repeatedly submitting a wrong password is how accounts lock.
    _check_for_credential_rejection(state.page, state.config, idp=state.idp)


def _sees_credential_picker(state: LoginState) -> bool:
    """Entra's PRIMARY picker -- which credential to sign in with.

    **Told apart from the verification picker by the attributes that actually
    differ**, both measured 2026-09-11: primary tiles are keyed by
    `data-test-cred-id`, verification tiles by `data-value` (an
    `authMethodId`). Requiring no verification tiles here, and no primary
    picker there, makes the two mutually exclusive rather than order-dependent
    -- so neither can be mistaken for the other on a page carrying both.
    """
    return (_present(state, ENTRA_CRED_PICKER)
            and not _entra_factor_offered(state.page))


def _answer_credential_picker(state: LoginState) -> None:
    if state.config.passwordless:
        # **The app IS the credential here**, so there is no password step at
        # all: nothing is typed afterwards and none is stored.
        if not _choose_primary_method(state.page, ENTRA_APP_TILE,
                                      ENTRA_APP_WORDS):
            snapshot = save_debug_snapshot(state.page, "login-no-passwordless")
            raise LoginError(
                f"Passwordless sign-in was asked for, but this account was not "
                f"offered an authenticator app as a primary credential "
                f"(url={state.page.url}). Drop --passwordless to use the stored "
                f"password instead. Snapshot: {snapshot}")
        state.awaiting_human = True
        _log("Passwordless sign-in: approve the request in Microsoft "
             "Authenticator -- match the number shown below.")
        _settle(state.page)
        _report_pending_factor(state.page)
        return

    # **"No password box" is not the end of the road.** A tenant whose default
    # method is a passkey shows that first; the password lives behind "Sign in
    # another way". Found only by going one screen further -- the whole UCF flow
    # was called impossible on the strength of the first screen it rendered.
    if not _choose_password_method(state.page):
        _raise_entra_stall(state.page, state.idp)


def _sees_method_picker(state: LoginState) -> bool:
    """Entra's verification tiles -- which factor to answer with."""
    return (not _present(state, ENTRA_CRED_PICKER)
            and bool(_entra_factor_offered(state.page)))


def _answer_method_picker(state: LoginState) -> None:
    chosen = _choose_entra_factor(state.page, state.approver.name)
    if chosen is None:
        # `FidoKey` alone lands here, and correctly: it needs hardware this
        # process does not have, so there is nothing to fall back to.
        _refuse(state, "login-no-answerable-factor",
                "reached a verification step it cannot answer")
    state.awaiting_human = True
    _settle(state.page)
    if chosen == ENTRA_OTP:
        # The code box is the next state, recognised on its own.
        _log("Second factor: Microsoft Authenticator code.")
        return
    _log("Second factor: Microsoft Authenticator push. Approve it on your "
         "phone -- match the number shown below.")
    _report_pending_factor(state.page)


def _looks_like_a_rejected_username(state: LoginState) -> bool:
    """We typed a username, waited, and the box is *still* asking for one.

    **Deliberately NOT a recogniser**, and the reason is worth keeping: it
    would fire on a perfectly healthy login. Entra's view switch is client-side
    and takes the better part of a second, during which the username box is
    still operable and the password box is not -- so a recogniser saying "the
    username box is still there" matches the ordinary flow mid-switch and
    refuses a sign-in that was about to work.

    The conclusion is only sound *after* the wait has been spent, which is
    exactly where `_refuse` stands. Recognisers answer what is on screen now;
    this answers what is still on screen after nothing else happened.
    """
    if not state.idp.two_screen or "a username" not in state.fired:
        return False
    try:
        return _operable(state, state.idp.username_field)
    except PlaywrightError:
        return False


def _sees_code_box(state: LoginState) -> bool:
    return _present(state, ENTRA_OTP_FIELD)


def _answer_code_box(state: LoginState) -> None:
    state.awaiting_human = True
    _complete_entra_otp(state.page)


#: Every page state this build can answer.
#:
#: **A set, not a script.** Adding a provider's new screen is a row here, not a
#: re-plumbing of a sequence -- the same reasoning that made `IDPS` a table.
#: Shibboleth is not an exception to the model: it collapses several states into
#: one form and hands the rest to Duo, which is one recogniser matching a page
#: that happens to hold two fields.
RECOGNISERS: tuple[Recogniser, ...] = (
    Recogniser("a Duo challenge", _sees_duo, _answer_duo,
               challenges_human=True),
    Recogniser("a sign-in form", _sees_single_screen_form,
               _answer_single_screen_form),
    Recogniser("a username", _sees_username_box, _answer_username_box),
    Recogniser("a password", _sees_password_box, _answer_password_box),
    Recogniser("an authenticator code", _sees_code_box, _answer_code_box,
               challenges_human=True),
    Recogniser("which credential to use", _sees_credential_picker,
               _answer_credential_picker),
    Recogniser("which verification method to use", _sees_method_picker,
               _answer_method_picker, challenges_human=True),
)


#: Entra's second-factor tiles. `data-value` carries the `authMethodId` straight
#: out of `$Config.arrUserProofs`, so this is the method's own name rather than
#: the English words next to it -- measured at UCF 2026-09-11.
ENTRA_FACTOR_TILES = '[role=button][data-value]'

#: Push with number matching: the page displays a number, the human taps it in
#: the Authenticator app, and the page polls (`oPerAuthPollingInterval` is 1s)
#: until it is approved. **Duo's shape exactly**, which is why the same seam
#: serves both.
#:
#: **The other two registered methods are deliberately not driven.** `FidoKey`
#: needs hardware this process does not have. `PhoneAppOTP` *could* be
#: automated -- and must not be: storing the TOTP seed would put both factors
#: in one file, which is the one thing `auth.py` has refused since it was
#: written. See "Auth model" in CONVENTIONS.
ENTRA_PUSH = "PhoneAppNotification"


#: The Authenticator's rotating code, typed by a human. Measured 2026-09-11.
#: `maxlength=6`, `inputmode=numeric`, labelled "Code"; the submit reads
#: "Verify".
ENTRA_OTP = "PhoneAppOTP"
ENTRA_OTP_FIELD = "input[name='otc']"
ENTRA_OTP_SUBMIT = "#idSubmit_SAOTCC_Continue"

#: Which `authMethodId` each `--factor` maps to. **The flag already existed for
#: Duo**, and reusing it keeps one vocabulary for "how do you want to answer the
#: second factor" rather than inventing a parallel one per IdP.
ENTRA_FACTOR_FOR = {"push": ENTRA_PUSH, "passcode": ENTRA_OTP}


def _entra_factor_offered(page: Page) -> list[str]:
    """Which second factors this IdP is offering, by `authMethodId`."""
    try:
        return page.eval_on_selector_all(
            ENTRA_FACTOR_TILES,
            "els => els.map(e => e.getAttribute('data-value')).filter(Boolean)")
    except PlaywrightError:
        return []


def _choose_entra_factor(page: Page, prefer: str = "push") -> str | None:
    """Pick a second factor. Returns the `authMethodId` chosen, or None.

    Chosen **by `authMethodId`, never by the words on the tile** -- the same
    value-not-label rule that `grading_type` taught this project, arriving at
    the IdP.

    Falls back to the other supported method when the preferred one is not
    registered, because refusing a login over a flag default helps nobody --
    but it says which it took, since the two need different things from the
    human (a phone tap versus a typed code).
    """
    offered = _entra_factor_offered(page)
    if not offered:
        return None

    wanted = ENTRA_FACTOR_FOR.get(prefer, ENTRA_PUSH)
    order = [wanted] + [m for m in (ENTRA_PUSH, ENTRA_OTP) if m != wanted]
    for method in order:
        if method not in offered:
            continue
        tile = page.locator(f'[role=button][data-value="{method}"]')
        if tile.count() != 1:
            continue  # ambiguous: refuse rather than choose
        if method != wanted:
            _log(f"{wanted} is not registered on this account; using {method}.")
        tile.first.click()
        return method

    # `FidoKey` lands here, and correctly: it needs hardware this process does
    # not have, so there is nothing to fall back to.
    _log(f"This account offers {', '.join(offered)}; none is a factor this "
         f"tool can answer.")
    return None


def _complete_entra_otp(page: Page, timeout_ms: int = 120_000) -> None:
    """Prompt for the Authenticator's code and submit it.

    **Nothing is stored.** The code is read from the terminal, typed into the
    form, and forgotten -- so this stays genuinely two-factor. Automating it
    from a stored TOTP seed would put both factors in one file, which is the
    line `auth.py` has held since it was written; see "Auth model".

    Read through `/dev/tty` rather than stdin, like every other interactive
    prompt here: a piped or `sshpass`-driven run has a terminal even when stdin
    is not one.
    """
    from .credentials import _read_from_tty

    try:
        page.wait_for_selector(ENTRA_OTP_FIELD, timeout=30_000)
    except PlaywrightTimeout as exc:
        snapshot = save_debug_snapshot(page, "entra-otc-missing")
        raise LoginError(
            f"Chose the Authenticator code method, but no code box appeared "
            f"(url={page.url}). Snapshot: {snapshot}") from exc

    code = _read_from_tty(
        "  Enter the 6-digit code from Microsoft Authenticator: ").strip()
    if not code:
        raise LoginError("No code entered; nothing was submitted.")

    page.fill(ENTRA_OTP_FIELD, code)
    submit = page.locator(ENTRA_OTP_SUBMIT)
    if submit.count() == 1:
        submit.first.click()
    else:
        # The form submits on Enter here. Safe in a way it is NOT on Canvas's
        # edit form, where Enter saves a half-typed row -- this page holds one
        # field and one action.
        page.press(ENTRA_OTP_FIELD, "Enter")
    page.wait_for_load_state("domcontentloaded")


#: Where Entra's number-match code might be. **UNMEASURED, and treated as a
#: guess throughout.** Nobody here has read this markup: the one live UCF login
#: rendered the number through the text dump below, and provoking another means
#: ringing a real person's phone, which is not a thing to do for a selector.
#:
#: So this is candidates-and-shrug. A hit renders the number in the box; a miss
#: costs nothing, because the box is drawn either way and the page's own text
#: still follows it. What must NOT happen is a bogus number -- telling someone
#: to tap `1` scraped from unrelated markup is worse than telling them nothing,
#: which `duo._scan_for_number` learned by doing it.
#:
#: **No digit-scan fallback here, deliberately**, though Duo has one: Duo's
#: codes are three digits and Entra's are two, and a bare two-digit line is far
#: likelier to match something that is not the code.
ENTRA_NUMBER_SELECTORS = (
    "#idRichContext_DisplaySign",
    "[data-bind*='displaySign']",
    "[class*='displaySign']",
)


def _read_entra_number(page: Page) -> str | None:
    """The number Entra is displaying for the human to match, if we can find it.

    Absence is normal and never an error -- see `ENTRA_NUMBER_SELECTORS`.
    """
    for selector in ENTRA_NUMBER_SELECTORS:
        try:
            text = page.locator(selector).first.inner_text(timeout=1_000)
        except Exception:                                       # noqa: BLE001
            continue
        digits = "".join(ch for ch in text if ch.isdigit())
        # A number match is short. Anything longer is some other element that
        # happened to match, and passing it on would be the bogus-code failure.
        if digits and len(digits) <= 3:
            return digits
    return None


def _report_pending_factor(page: Page) -> None:
    """Print what the IdP is showing, when it is showing something we cannot drive.

    **The screen is the only place a number-match code exists.** Entra's
    "Approve a request on my Microsoft Authenticator app" displays a two-digit
    number that the human must type into the app -- and it is rendered in a
    headless browser nobody can see, so without this the run is unapprovable
    even with the person sitting right there.

    **Two halves, and only one of them depends on a selector.**

    1. A **box, in Duo's style** (user's call, 2026-09-11). Duo's number gets a
       framed, charset-aware box and Entra's arrived as an unremarkable line
       among nine -- easy to miss in a ~60s window, on someone else's account.
       The box is drawn *whether or not* the number was found, so the prompt
       itself is unmissable even when the guessed selector misses; that half is
       guaranteed.
    2. The page's **own visible text**, exactly as before. Deliberately kept:
       this project has paid for confident guesses about markup nobody had
       measured, and printing the text needs no guess and works for factors
       this code has never met. The rule when this was written was that a
       tidier renderer "must not print less" -- so it prints strictly more.
    """
    number = _read_entra_number(page)
    print_box(
        code_box(
            number,
            heading="Microsoft Authenticator: Approve",
            instruction="Approve in Authenticator",
            glyphs=glyphs_for(sys.stderr),
        ),
        sys.stderr,
    )
    _report_page_text(page)


def _report_page_text(page: Page) -> None:
    """Print the page's own visible words.

    **Needs no selector, and so works for states this code has never met** --
    which is why it is also what a refusal prints. Split out from
    `_report_pending_factor` when the login became a loop: a state we do not
    recognise must report itself, but must NOT be given the Authenticator box,
    which would tell the reader to approve something nobody asked for.
    """
    try:
        text = page.inner_text("body") or ""
    except PlaywrightError:
        return
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return
    _log("The identity provider is asking for something this tool cannot "
         "answer by itself. It says:")
    for line in lines[:10]:
        _log(f"    {line[:100]}")


def _wait_until_authenticated(
    page: Page, config: Config, timeout_ms: int = 30_000,
) -> bool:
    """Give the post-Duo redirect chain time to actually finish.

    Polls rather than sampling once: after a second factor the browser is still
    walking a redirect chain, so a single check runs mid-chain and reports
    failure for a login that is merely still in flight.
    """
    waited = 0
    reported = False
    while waited < timeout_ms:
        if _looks_authenticated(page.url, config):
            return True
        # Once, early, and only while we are still somewhere else: show the
        # human what the page wants. Printed before the wait is spent, not
        # after, because a code they see when it has expired is no use.
        if not reported and waited >= 2_000:
            _report_pending_factor(page)
            reported = True
        page.wait_for_timeout(1_000)
        waited += 1_000
    return False


def ensure_logged_in(page: Page, config: Config, approver: Approver | None = None) -> bool:
    """Guarantee an authenticated session. Returns True if a login was performed.

    Every task entry point should call this first. When the session is warm this
    costs one page load and involves no human at all.
    """
    if is_logged_in(page, config):
        _log("Existing session is still good.")
        return False
    log_in(page, config, approver)
    return True
