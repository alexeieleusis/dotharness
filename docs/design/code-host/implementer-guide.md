# Code host: implementer guide

Status: belongs to the Phase 0 design for #192 and is not implemented. Planned location of the code: `src/dotharness/code_host/`.

This guide is an ordered checklist for the author of a new code-host implementation. It states no contract. Each step points to
the section of the design page [`docs/design/code-host.md`](../code-host.md) that owns the contract, named by its exact
heading title (the headings are unnumbered). Terms such as Code host, Capability interface, Consumer, Reference implementation
and Anchor are used as the design page uses them; its "Terms used here" list (under Inventory) is the glossary. Examples use the
placeholder host `examplehost` and the token value `<token>`.

## 1. Choosing capabilities

1. List the capability interfaces in the design page section "Interfaces" (`Session`, `Repository`, `ChangeRequests`,
   `Discussion`, `Checks`, `WorkItems`).
2. Pick the subset that `examplehost` can honor.
3. Leave the others undeclared. An unimplemented capability is declared absent, not stubbed.

Pointers: "Interfaces"; "Declarations", subsection "Capabilities".

## 2. Declaring capabilities, limits and anchors

1. Build the one declaration value the host exposes. It carries three parts: the capability set, the limits and the anchor
   rules.
2. Declare the capabilities chosen in step 1.
3. Declare the limits (time and list bounds) the host enforces.
4. Declare the anchor rules for inline review comments, if `Discussion` is declared.

Pointers: "Declarations", subsections "Capabilities", "Limits", "Anchor and anchor rules"; "Reference implementation values"
shows what the reference implementation declares.

## 3. Mapping errors

1. Map every host failure onto the error types of the design page, including the three error distinctions and the
   unsupported-capability error.
2. Never place the token (`<token>`) or the token command in an error message.

Pointers: "Error model", subsections "Hierarchy", "Failure conventions per call site", "Unsupported-capability policy" and
"Message and secret rules".

## 4. Exposing the typed options model

1. Define the options model that validates the host's own settings.
2. Hand it to the settings layer through the registration value, so the layer passes the untyped options through and the model
   validates them.

Pointers: "Factory and selection", subsections "Factory" (rule on typed options) and "Extension mechanism".

## 5. Registering

1. By entry point: publish the registration value in the entry-point group the design page reserves, under the name
   `examplehost`.
2. By dotted path: point the selection at the module and attribute holding the registration, for example
   `examplehost_pkg.host` with the attribute the design page's dotted-path form requires.

Pointers: "Factory and selection", subsections "Factory", "Extension mechanism" and "Selection per tool"; "Reserved names"
lists the names you must not use.

## 6. Running the conformance suite

1. Install the dotharness wheel in the environment where `examplehost` is developed. The suite and the in-memory fake ship in
   the wheel.
2. Subclass the suite base class in your own test file and supply the one hook that returns a fresh target. Use `<token>`
   where a token value is needed.
3. Run the subclass with your test runner. A pass means the implementation behaves as the asserted behaviors describe for every
   capability it declares.
4. Consumer tests that need a host without a network can use the in-memory fake instead.

Pointers: "Conformance suite and in-memory fake", subsections "Module, packaging and run mechanism", "Behaviors asserted" and
"In-memory fake".

## 7. Stating the targeted interface version

1. State the interface version the host targets in its registration value.
2. When dotharness changes the version, re-run the suite and update the statement.

Pointer: "Factory and selection", subsection "Version and stability contract".

## 8. Checklist

- [ ] Capabilities chosen; the rest left undeclared ("Interfaces"; "Declarations", "Capabilities").
- [ ] Declaration written with limits and anchor rules ("Declarations").
- [ ] Errors mapped; no secret in any message ("Error model").
- [ ] Options model exposed through the registration ("Factory and selection", "Factory" and "Extension mechanism").
- [ ] Registered by entry point or dotted path ("Factory and selection", "Extension mechanism").
- [ ] Conformance suite passes ("Conformance suite and in-memory fake").
- [ ] Targeted interface version stated ("Factory and selection", "Version and stability contract").
