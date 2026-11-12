# Code host: implementer guide

Status: this guide belongs to the Phase 0 design for #192. The package `src/dotharness/code_host/` exists and the vibe-heal review flow is implemented (Phase 1, #193). Code location: `src/dotharness/code_host/`.

This guide is an ordered checklist for the author of a new code-host implementation. It states no contract. Each step points to
the section of the design page [`docs/design/code-host.md`](../code-host.md) that owns the contract. The pointer names the section
by its exact heading title (the headings are unnumbered).

This guide uses terms such as Code host, Capability interface, Consumer, Reference implementation and Anchor the way the design
page uses them. The design page's "Terms used here" list (under Inventory) is the glossary. Examples use the placeholder host
`examplehost` and the token value `<token>`.

## 1. Choosing capabilities

1. List the capability interfaces in the design page section "Interfaces" (`Session`, `Repository`, `ChangeRequests`,
   `Discussion`, `Checks`, `WorkItems`).
2. Pick the subset that `examplehost` can honor.
3. Leave the others undeclared. An unimplemented capability is declared absent, not stubbed.

Pointers: "Interfaces" and "Declarations", subsection "Capabilities".

## 2. Declaring capabilities, limits and anchors

1. Build the one declaration value the host exposes. It carries three parts: the capability set, the limits and the anchor
   rules.
2. Declare the capabilities chosen in section 1.
3. Declare the limits (time and list bounds) the host enforces.
4. Declare the anchor rules for inline review comments, if `Discussion` is declared.

Pointers: "Declarations" (subsections "Capabilities", "Limits" and "Anchor and anchor rules") and "Reference implementation
values", which shows what the reference implementation declares.

## 3. Mapping errors

1. Map every host failure onto the design page's error types, including the three error distinctions and the
   unsupported-capability error.
2. Never place the token (`<token>`) or the token command in an error message.
3. Raise `OSError` from `for_current_branch` and `post_review` when the host client is not installed. `Session.available()` returns
   `False` instead and never raises.
4. Return `reason` in `ReviewPostResult` only when `fell_back` is true.

Pointers: "Error model", subsections "Hierarchy", "Failure conventions per call site", "Unsupported-capability policy" and
"Message and secret rules".

## 4. Exposing the typed options model

1. Define the options model that validates the host's own settings.
2. Pass it to the settings layer through the registration value. The layer passes the untyped options through, and the model
   validates them.

Pointers: "Factory and selection", subsections "Factory" (rule on typed options) and "Extension mechanism".

## 5. Registering

1. By entry point: publish the registration value in the entry-point group the design page reserves, under the name
   `examplehost`.
2. By dotted path: point the selection at the module and attribute holding the registration. For example, use
   `examplehost_pkg.host` with the attribute the design page's dotted-path form requires.

Pointers: "Factory and selection" (subsections "Factory", "Extension mechanism" and "Selection per tool") and "Reserved
names", which lists the names you must not use.

## 6. Running the conformance suite

1. Install the dotharness wheel in the environment where `examplehost` is developed. The suite and the in-memory fake are
   included in the wheel.
2. Subclass the suite base class in your own test file. Supply the one hook that returns a fresh target. Use `<token>`
   where a token value is needed.
3. Run the subclass with your test runner. A pass means the implementation behaves as the asserted behaviors describe for every
   capability it declares.
4. The review-flow behaviors (B-NCR, B-REV, B-LIM, B-SEC) are in the same base class. The seeder you supply stubs your
   transport and records posts (`HostSeeder` in `dotharness.code_host.testing`).
5. Consumer tests that need a host without a network can use the in-memory fake instead.

Pointers: "Conformance suite and in-memory fake", subsections "Module, packaging and run mechanism", "Behaviors asserted" and
"In-memory fake".

## 7. Stating the targeted interface version

1. State the interface version the host targets in its registration value.
2. When dotharness changes the version, re-run the suite and update the statement.

Pointer: "Factory and selection", subsection "Version and stability contract".

## 8. Checklist

- [ ] The capabilities are chosen, and the rest are left undeclared ("Interfaces" and "Declarations", "Capabilities").
- [ ] The declaration is written with limits and anchor rules ("Declarations").
- [ ] The errors are mapped, and no secret appears in any message ("Error model").
- [ ] The options model is exposed through the registration ("Factory and selection", "Factory" and "Extension mechanism").
- [ ] The host is registered by entry point or dotted path ("Factory and selection", "Extension mechanism").
- [ ] Conformance suite passes ("Conformance suite and in-memory fake").
- [ ] The targeted interface version is stated ("Factory and selection", "Version and stability contract").
