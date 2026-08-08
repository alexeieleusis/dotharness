# Quick Reference: Component Organization Patterns

**Ultra-condensed reference for day-to-day development.**

**Note on Storybook:** Storybook is a **catalog for manual verification/QA** of components, not an automated test suite. For automated coverage - including interaction and screenshot tests - see [Testing Priority](#testing-priority) below and [COMPONENT_ORGANIZATION_CONVENTION.md § Testing Strategy](./COMPONENT_ORGANIZATION_CONVENTION.md#testing-strategy).

---

## File Structure

```
feature-name/
├── feature-name.tsx                    # Container and presentation (both exported)
├── feature-name.stories.tsx            # Storybook - manual verification catalog (optional)
├── use-feature-name.ts                 # Orchestrator (boring)
├── use-feature-name-form.ts            # Form logic
├── use-feature-name-state.ts           # UI state
├── use-feature-name-mutations.ts       # API mutations
├── use-feature-name-actions.ts         # User interactions
├── use-feature-name-domain.ts          # Business logic (pure functions)
├── types.ts                            # All types
└── __tests__/
    ├── feature-name-domain.test.ts      # HIGHEST PRIORITY
    ├── use-feature-name-*.test.ts       # Hook tests
    ├── feature-name.test.tsx            # Component test (happy-dom, optional)
    ├── feature-name.component.spec.tsx  # Interaction test (real browser, optional)
    └── feature-name.screenshot.spec.tsx # Screenshot test (real browser, optional)
```

---

## Dependency Flow

```
Domain Functions (pure, no React)
  ↓
Props
  ↓
Form Hook (no dependencies)
  ↓
State Hook (depends on form data)
  ↓
Query Hooks (from consolidated files)
  ↓
Mutations Hook (uses domain functions)
  ↓
Actions Hook (curries domain functions with state)
  ↓
Orchestrator Hook (wires everything)
  ↓
Container (calls hook)
  ↓
Presentation (renders props)
```

---

## Core Principles (The 10 Commandments)

1. **Business logic → domain functions** (pure, testable, no React)
2. **Actions curry domain functions** (no business logic in actions)
3. **Main hook is boring** (just wiring, no logic)
4. **Container is boring** (just calls hook and renders presentation)
5. **Presentation is pure** (props = hook return type, no hooks)
6. **Domain tests are highest priority** (then hooks, then integration)
7. **Never expose mutation objects** (wrap in simple functions)
8. **Linear dependencies** (each hook depends only on previous ones)
9. **Storybook is optional** (skip if mocking is complex)
10. **Query hooks are consolidated** (not feature-specific)

---

## Pattern Templates

### Component File (Container + Presentation)

```typescript
// feature-name.tsx
import { useFeatureName } from './use-feature-name';
import type { FeatureNameProps, UseFeatureNameHook } from './types';

// Presentation - exported for Storybook
export function FeatureNamePresentation(props: UseFeatureNameHook) {
  const { form, state, actions, data } = props;
  return <JSX />;
}

// Container - used in application
export function FeatureName(props: FeatureNameProps): ReactElement {
  const hookResult = useFeatureName(props);
  return <FeatureNamePresentation {...hookResult} />;
}
```

### Domain Functions

```typescript
// use-feature-name-domain.ts
// Pure functions only - no React, no side effects

export function validateFormData(formData: FormData) {
  // Return { isValid: boolean; errors: string[] }
}

export function transformFormDataToRequest(formData: FormData) {
  // Return API request format
}

export function canDeleteEntity(entity: Entity, related: Entity[]) {
  // Return { canDelete: boolean; reason?: string }
}
```

### Orchestrator Hook

```typescript
// use-feature-name.ts
export function useFeatureName(props: Props): UseFeatureNameHook {
  const form = useFeatureNameForm({ existingEntity: props.existingEntity });
  const stateInternal = useFeatureNameState({ formData: form.watch() });
  const { data: entities = [], isLoading } = useEntities(); // From consolidated file
  const mutations = useFeatureNameMutations({ onSuccess: props.onClose });
  const actions = useFeatureNameActions({
    form,
    state: stateInternal, // Pass internal state with setters to actions
    data: { entities, isLoading },
    mutations,
  });

  // Filter out setters before returning
  const { setIsDialogOpen, setEditingIndex, ...publicState } = stateInternal;

  return { form, state: publicState, actions, data: { entities, isLoading } };
}
```

### Actions Hook

```typescript
// use-feature-name-actions.ts
import {
  validateFormData,
  transformFormDataToRequest,
} from './use-feature-name-domain';

export function useFeatureNameActions(props: ActionsProps): Actions {
  const handleSave = useCallback(async () => {
    // 1. Use domain function for validation
    const validation = validateFormData(props.form.control._formValues);
    if (!validation.isValid) {
      validation.errors.forEach((error) => showToast('error', error));
      return;
    }

    // 2. Use domain function for transformation
    const requestData = transformFormDataToRequest(
      props.form.control._formValues,
    );

    // 3. Call mutation
    await props.mutations.create(requestData);
  }, [props.form, props.mutations]);

  return { handleSave };
}
```

---

## Type Structure

```typescript
// types.ts

// Container props
export type FeatureNameProps = {
  onClose?: () => void;
  existingEntity?: Entity;
};

// Form data
export type FeatureNameFormData = {
  field1: string;
  field2: number;
};

// Progressive hook props pattern - each hook gets original props + previous results
export type UseFeatureNameFormProps = Pick<FeatureNameProps, 'existingEntity'>;

export type UseFeatureNameStateProps = FeatureNameProps & {
  form: FeatureNameFormControls;
};

export type UseFeatureNameMutationsProps = UseFeatureNameStateProps & {
  state: FeatureNameState;
};

export type UseFeatureNameActionsProps = UseFeatureNameMutationsProps & {
  form: FeatureNameFormControls;
  state: FeatureNameStateInternal; // Internal state with setters - actions call them
  data: FeatureNameData;
  mutations: FeatureNameMutations;
};

// Hook return types (grouped by responsibility)
export type FeatureNameFormControls = {
  control: Control<FeatureNameFormData>;
  handleSubmit: UseFormHandleSubmit<FeatureNameFormData>;
  errors: FieldErrors<FeatureNameFormData>;
};

// Public state (exposed to component)
export type FeatureNameState = {
  isDialogOpen: boolean;
  editingIndex: number | null;
};

// Internal state (used by actions hook, includes setters)
export type FeatureNameStateInternal = FeatureNameState & {
  setIsDialogOpen: (open: boolean) => void;
  setEditingIndex: (index: number | null) => void;
};

export type FeatureNameActions = {
  handleSave: () => void;
  handleDelete: (id: string) => void;
};

export type FeatureNameData = {
  entities: Entity[];
  isLoading: boolean;
};

// Main hook interface
export type UseFeatureNameHook = {
  form: FeatureNameFormControls;
  state: FeatureNameState;
  actions: FeatureNameActions;
  data: FeatureNameData;
};

// Presentation props = hook return type
export type FeatureNamePresentationProps = UseFeatureNameHook;
```

---

## Decision Trees

### Should I create Storybook stories?

```
Is the presentation component easy to mock?
├─ Yes → Create stories
│  ├─ Simple form controls? → Easy to mock
│  ├─ Basic data structures? → Easy to mock
│  └─ Standard MUI components? → Easy to mock
│
└─ No → Skip stories, focus on domain tests
   ├─ Complex pre-existing components? → Hard to mock
   ├─ Complex form state? → Hard to mock
   └─ Heavy data dependencies? → Hard to mock
```

### Creating new or migrating?

```
Do I have existing code?
├─ No → Follow "Creating New Component"
│  └─ Bottom-up: types → domain → hooks → components
│
└─ Yes → Follow "Migration Strategy"
   └─ Top-down: extract types → extract domain → split hooks
```

---

## Testing Priority

1. **Domain functions** (HIGHEST) - Pure functions, fast, comprehensive
2. **Hook unit tests** - Each hook with mocked dependencies
3. **Integration tests** - Main hook with real sub-hooks
4. **Component tests** (`.test.tsx`, `happy-dom`) - Non-trivial rendering logic, optional
5. **Interaction tests** (`.component.spec.tsx`, real browser) - Behavior `happy-dom` can't fake: hover, focus, real layout, optional
6. **Screenshot tests** (`.screenshot.spec.tsx`, real browser) - Pixel-level visual regression, optional

**Storybook stories** are a separate, optional manual verification catalog - not part of the automated testing priority above.

**Always prioritize domain tests over everything else.**

---

## Common Patterns

### Form Hook Pattern

```typescript
export function useFeatureNameForm(props: FormProps): FormControls {
  const {
    control,
    handleSubmit,
    formState: { errors },
  } = useOscForm({
    resolver: yupResolver(schema),
    defaultValues: getDefaults(),
  });
  return { control, handleSubmit, errors };
}
```

### State Hook Pattern

```typescript
export function useFeatureNameState(props: StateProps): StateInternal {
  const [isOpen, setIsOpen] = useState(false);

  // Use domain functions for computed state
  const computed = useMemo(
    () => computeFromDomain(props.formData),
    [props.formData],
  );

  return { isOpen, computed, setIsOpen };
}
```

### Mutations Hook Pattern

```typescript
export function useFeatureNameMutations(props: MutationsProps): Mutations {
  const mutation = useMutation({
    mutationFn: apiCall,
    onSuccess: () => {
      showToast('success');
      invalidateQueries(['key']);
      props.onSuccess?.();
    },
  });

  // Hide mutation object
  return {
    create: useCallback((data) => mutation.mutateAsync(data), [mutation]),
    isCreating: mutation.isLoading,
  };
}
```

### Storybook Mock Helper

```typescript
function createMockProps(overrides?: Partial<UseFeatureNameHook>) {
  return {
    form: { control: {} as any, handleSubmit: () => () => {}, errors: {} },
    state: { isDialogOpen: false },
    actions: { handleSave: action('handleSave') },
    data: { entities: [], isLoading: false },
    ...overrides,
  };
}
```

---

## Quick Checks

### ✅ Good Patterns

```typescript
// ✅ Domain function is pure
export function validateData(data: Data) {
  return { isValid: data.field.length > 0 };
}

// ✅ Action curries domain function
const handleSave = useCallback(() => {
  const validation = validateData(formData);
  if (!validation.isValid) return;
  // ...
}, [formData]);

// ✅ Main hook is boring
export function useFeature(props) {
  const form = useFeatureForm();
  const state = useFeatureState({ formData: form.watch() });
  const actions = useFeatureActions({ form, state });
  return { form, state, actions };
}

// ✅ Container is boring (in same file as presentation)
export function Feature(props) {
  const hookResult = useFeature(props);
  return <FeaturePresentation {...hookResult} />;
}

// ✅ Presentation is exported for Storybook
export function FeaturePresentation(props: UseFeatureHook) {
  return <JSX />;
}
```

### ❌ Anti-Patterns

```typescript
// ❌ Domain function has side effects
export function validateData(data: Data) {
  showToast('Validating...'); // ❌ Side effect
  return { isValid: true };
}

// ❌ Business logic in action
const handleSave = useCallback(() => {
  if (!formData.field || formData.field.trim() === '') { // ❌ Validation logic
    return;
  }
  const request = { name: formData.field.trim() }; // ❌ Transformation logic
  // ...
}, [formData]);

// ❌ Main hook has logic
export function useFeature(props) {
  const [isOpen, setIsOpen] = useState(false); // ❌ Should be in state hook
  const handleClick = () => { /* ... */ }; // ❌ Should be in actions hook
  return { isOpen, handleClick };
}

// ❌ Container has logic
export function Feature(props) {
  const hookResult = useFeature(props);
  const [localState, setLocalState] = useState(); // ❌ Logic in container
  return <FeaturePresentation {...hookResult} />;
}
```

---

## Cheat Sheet: What Goes Where

| Concern            | File                 | Contains                           | No React Hooks? | Pure?             |
| ------------------ | -------------------- | ---------------------------------- | --------------- | ----------------- |
| **Business logic** | `use-*-domain.ts`    | Validation, transformations, rules | ✅ Yes          | ✅ Yes            |
| **Form setup**     | `use-*-form.ts`      | useOscForm, field arrays           | ❌ No           | ❌ No             |
| **UI state**       | `use-*-state.ts`     | useState, useMemo for derived      | ❌ No           | ❌ No             |
| **API mutations**  | `use-*-mutations.ts` | useMutation, error handling        | ❌ No           | ❌ No             |
| **User actions**   | `use-*-actions.ts`   | Event handlers, currying           | ❌ No           | ❌ No             |
| **Orchestration**  | `use-*.ts`           | Calls other hooks                  | ❌ No           | ❌ No             |
| **Presentation**   | `*.tsx` (exported)   | JSX, styling                       | ✅ Yes          | ✅ Yes (of props) |
| **Container**      | `*.tsx` (exported)   | Calls main hook                    | ❌ No           | ❌ No             |

---

## When Stuck

1. **"Where does this logic go?"** → Is it pure? Domain. Uses React? Appropriate hook.
2. **"Should I create stories?"** → Is mocking easy? Yes → stories. No → skip.
3. **"Hook getting too big?"** → Split by responsibility (form/state/mutations/actions).
4. **"Creating or migrating?"** → New → bottom-up. Existing → extract then split.
5. **"Domain tests or Storybook?"** → Domain tests always highest priority.

See [Core Principles (The 10 Commandments)](#core-principles-the-10-commandments) above for the full list to remember.
