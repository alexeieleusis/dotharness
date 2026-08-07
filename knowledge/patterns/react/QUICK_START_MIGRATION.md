# Quick Start: Migrating an Existing Component

**Use this guide when refactoring existing code to follow the component organization pattern.**

**Note on Storybook:** Storybook is a **catalog for manual verification/QA** of components, not an automated test suite. Use stories to browse component states and click through them by hand. For automated coverage - including interaction and screenshot tests - see [Step 6: Add Hook Tests](#step-6-add-hook-tests) and [COMPONENT_ORGANIZATION_CONVENTION.md § Testing Strategy](./COMPONENT_ORGANIZATION_CONVENTION.md#testing-strategy).

## Key Differences from Creating New

| Aspect             | Creating New                       | Migrating Existing                      |
| ------------------ | ---------------------------------- | --------------------------------------- |
| **Starting point** | Clean slate                        | Working code with implicit logic        |
| **Approach**       | Bottom-up (types → domain → hooks) | Top-down (extract → split → reorganize) |
| **Testing**        | Write tests as you build           | Preserve existing behavior              |
| **Risk**           | Low                                | Medium - must maintain functionality    |
| **Speed**          | Faster                             | Slower - must understand first          |
| **Storybook**      | Evaluate early                     | Often skip until after core refactoring |

**Migration is exploratory** - discovering what the code does while reorganizing.
**New development is constructive** - building with clear structure from the start.

---

## Migration Strategy

### Principles

- **Work incrementally** - Keep code working at each step
- **Extract before rewrite** - Identify logic before reorganizing
- **Test after each step** - Ensure existing functionality preserved
- **Domain logic first** - Extract business logic before splitting hooks
- **Backward compatibility** - Component interface can stay the same during refactoring

---

## Step-by-Step Process

### Step 1: Extract Types

**Goal:** Centralize all type definitions.

1. Create `types.ts`
2. Extract all `type` and `interface` definitions from component and hooks
3. Export everything
4. Update imports in component and hooks
5. **Test:** Code still compiles and runs

```typescript
// types.ts
export type FeatureNameProps = {
  /* ... */
};

export type FeatureNameFormData = {
  /* ... */
};

// Progressive hook props pattern
export type UseFeatureNameFormProps = Pick<
  FeatureNameProps,
  'existingEntity' | 'initialData'
>;

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

// Hook return types
export type FeatureNameFormControls = {
  /* ... */
};

// Public state (exposed to component)
export type FeatureNameState = {
  /* ... */
};

// Internal state (used by actions hook, includes setters)
export type FeatureNameStateInternal = FeatureNameState & {
  setIsDialogOpen: (open: boolean) => void;
  setEditingIndex: (index: number | null) => void;
  /* ... other setters */
};

export type FeatureNameActions = {
  /* ... */
};
export type FeatureNameData = {
  /* ... */
};
export type FeatureNameMutations = {
  /* ... */
};

export type UseFeatureNameHook = {
  form: FeatureNameFormControls;
  state: FeatureNameState; // Public state only
  actions: FeatureNameActions;
  data: FeatureNameData;
};
```

---

### Step 2: Extract Business Logic to Domain Functions

**Goal:** Identify and extract all business logic into pure functions.

**Look for:**

- Validation logic (inline checks, custom validators)
- Data transformations (form data → API format)
- Business rules (can delete? can edit? eligibility checks)
- Calculations (derived values, computations)

**Example - Before:**

```typescript
// Inline in component/hook
const handleSave = async () => {
  if (!formData.field1 || formData.field1.trim() === '') {
    showToast('error', 'Field 1 is required');
    return;
  }

  const requestData = {
    name: formData.field1.trim(),
    value: formData.field2,
  };

  await createMutation.mutateAsync(requestData);
};
```

**After - Extract to domain:**

```typescript
// use-feature-name-domain.ts
export function validateFormData(formData: FeatureNameFormData) {
  const errors: string[] = [];
  if (!formData.field1 || formData.field1.trim() === '') {
    errors.push('Field 1 is required');
  }
  return { isValid: errors.length === 0, errors };
}

export function transformFormDataToRequest(formData: FeatureNameFormData) {
  return {
    name: formData.field1.trim(),
    value: formData.field2,
  };
}

// use-feature-name-actions.ts
const handleSave = async () => {
  const validation = validateFormData(formData);
  if (!validation.isValid) {
    validation.errors.forEach((e) => showToast('error', e));
    return;
  }

  const requestData = transformFormDataToRequest(formData);
  await createMutation.mutateAsync(requestData);
};
```

**Write domain tests immediately:**

```typescript
// __tests__/feature-name-domain.test.ts
describe('validateFormData', () => {
  it('should validate correct data', () => {
    const result = validateFormData({ field1: 'test', field2: 10 });
    expect(result.isValid).toBe(true);
  });

  it('should catch validation errors', () => {
    const result = validateFormData({ field1: '', field2: 10 });
    expect(result.isValid).toBe(false);
    expect(result.errors).toContain('Field 1 is required');
  });
});
```

**Test:** Existing functionality still works, domain tests pass.

---

### Step 3: Split Hooks

**Goal:** Separate concerns into focused hooks.

#### 3a. Extract Form Hook

**Look for:**

- `useForm` / `useOscForm` calls
- `useFieldArray` calls
- Form validation schemas
- Field registration

```typescript
// use-feature-name-form.ts
export function useFeatureNameForm(props) {
  const {
    control,
    handleSubmit,
    formState: { errors },
    setValue,
  } = useOscForm<FeatureNameFormData>({
    resolver: yupResolver(validationSchema),
    defaultValues: getDefaultValues(),
  });

  return { control, handleSubmit, errors, setValue };
}
```

**Test:** Form still works as before.

---

#### 3b. Extract State Hook

**Look for:**

- `useState` calls for UI state (modals, editing indices, selections)
- `useMemo` for computed/derived state
- `useEffect` for state synchronization

```typescript
// use-feature-name-state.ts
export function useFeatureNameState(
  props: UseFeatureNameStateProps,
): FeatureNameStateInternal {
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);

  // Use domain functions for computed state
  const computedValue = useMemo(
    () => computeDerivedValue(props.formData.field1),
    [props.formData.field1],
  );

  return {
    // Public state values
    isDialogOpen,
    editingIndex,
    computedValue,
    // Setters (will be filtered out by orchestrator)
    setIsDialogOpen,
    setEditingIndex,
  };
}
```

**Test:** UI state management still works.

---

#### 3c. Extract Mutations Hook

**Look for:**

- `useMutation` calls
- API call logic
- Success/error handlers
- Toast notifications
- Query invalidation

```typescript
// use-feature-name-mutations.ts
export function useFeatureNameMutations(props) {
  const showToast = useToast();
  const invalidateQueries = useEnvQueryInvalidator();

  const createMutation = useMutation({
    mutationFn: createEntity,
    onSuccess: () => {
      showToast('success', 'Created successfully');
      invalidateQueries(['getEntities']);
      props.onSuccess?.();
    },
  });

  // Hide mutation objects - expose simple functions
  return {
    create: useCallback(
      (data) => createMutation.mutateAsync(data),
      [createMutation],
    ),
    isCreating: createMutation.isLoading,
  };
}
```

**Test:** API interactions still work.

---

#### 3d. Extract Actions Hook

**Look for:**

- Event handlers (`handleClick`, `handleSubmit`, etc.)
- User interaction logic
- Coordination between form, state, and mutations

**Important:** Actions should **curry domain functions** with hook state, not contain business logic.

```typescript
// use-feature-name-actions.ts
import {
  validateFormData,
  transformFormDataToRequest,
} from './use-feature-name-domain';

export function useFeatureNameActions(props) {
  const showToast = useToast();

  const handleSave = useCallback(async () => {
    // Use domain function
    const validation = validateFormData(props.form.control._formValues);
    if (!validation.isValid) {
      validation.errors.forEach((e) => showToast('error', e));
      return;
    }

    // Use domain function
    const requestData = transformFormDataToRequest(
      props.form.control._formValues,
      props.data.entities,
    );

    await props.mutations.create(requestData);
  }, [props.form, props.data.entities, props.mutations, showToast]);

  return { handleSave };
}
```

**Test:** User interactions still work.

---

### Step 4: Refactor Main Hook to Orchestrator

**Goal:** Main hook becomes "boring" - just wiring, no logic.

**Before:**

```typescript
export function useFeatureName(props) {
  // Mix of everything: form setup, state, queries, mutations, handlers
  const { control, handleSubmit } = useOscForm({
    /* ... */
  });
  const [isOpen, setIsOpen] = useState(false);
  const mutation = useMutation({
    /* ... */
  });
  const handleSave = () => {
    /* complex logic */
  };
  // ...
  return { control, handleSubmit, isOpen, setIsOpen, handleSave };
}
```

**After:**

```typescript
export function useFeatureName(props): UseFeatureNameHook {
  // 1. Form
  const form = useFeatureNameForm({ existingEntity: props.existingEntity });

  // 2. State (depends on form) - returns internal state with setters
  const stateInternal = useFeatureNameState({ formData: form.watch() });

  // 3. Query hooks directly
  const { data: entities = [], isLoading } = useEntities();

  // 4. Mutations
  const mutations = useFeatureNameMutations({ onSuccess: props.onClose });

  // 5. Actions (depends on everything above, receives internal state with setters)
  const actions = useFeatureNameActions({
    form,
    state: stateInternal,
    data: { entities, isLoading },
    mutations,
  });

  // Filter out setters before returning to component
  const { setIsDialogOpen, setEditingIndex, ...publicState } = stateInternal;

  return { form, state: publicState, actions, data: { entities, isLoading } };
}
```

**Test:** Component still works end-to-end.

---

### Step 5: Split Component into Container/Presentation

**Goal:** Separate hook invocation from rendering in the same file.

#### Refactor Component File

**Update the component file** to export both presentation and container components.

```typescript
// feature-name.tsx
import { useFeatureName } from './use-feature-name';
import type { FeatureNameProps, UseFeatureNameHook } from './types';

/**
 * Pure presentation component - receives all data and callbacks as props
 * IMPORTANT: Must be exported for Storybook stories
 */
export function FeatureNamePresentation(
  props: UseFeatureNameHook,
): ReactElement {
  const { form, state, actions, data } = props;

  return (
    <Box>
      {/* All your existing JSX here */}
      <form onSubmit={form.handleSubmit(actions.handleSave)}>
        {/* ... */}
      </form>
    </Box>
  );
}

/**
 * Container component - connects hook to presentation
 */
export function FeatureName(props: FeatureNameProps): ReactElement {
  const hookResult = useFeatureName(props);
  return <FeatureNamePresentation {...hookResult} />;
}
```

**Test:** Component still works exactly as before, both visual and functional.

---

### Step 6: Add Hook Tests

Now that hooks are separated, write unit tests for each:

```typescript
// __tests__/use-feature-name-form.test.ts
describe('useFeatureNameForm', () => {
  it('should initialize with default values', () => {
    const { result } = renderHook(() => useFeatureNameForm({}));
    expect(result.current.control._defaultValues).toEqual({
      field1: '',
      field2: 0,
    });
  });
});

// __tests__/use-feature-name-actions.test.ts
describe('useFeatureNameActions', () => {
  it('should call domain validation before saving', async () => {
    const mockMutations = { create: vi.fn() };
    const mockForm = { control: { _formValues: { field1: '', field2: 0 } } };

    const { result } = renderHook(() =>
      useFeatureNameActions({
        form: mockForm,
        state: {},
        data: { entities: [] },
        mutations: mockMutations,
      }),
    );

    await result.current.handleSave();

    // Should not call mutation due to validation failure
    expect(mockMutations.create).not.toHaveBeenCalled();
  });
});
```

If the component itself has non-trivial rendering logic or behavior worth pinning, add:

- `feature-name.test.tsx` (optional) - RTL component test, `happy-dom`, for conditional rendering
- `feature-name.component.spec.tsx` (optional) - real-browser interaction test, for behavior `happy-dom` can't fake (hover, focus, real layout)
- `feature-name.screenshot.spec.tsx` (optional) - pixel-diff test against a baseline PNG, run through `pnpm run test:screenshot:docker` so baselines match CI

See [COMPONENT_ORGANIZATION_CONVENTION.md § Testing Strategy](./COMPONENT_ORGANIZATION_CONVENTION.md#testing-strategy) for examples and a "which test type do I reach for" decision guide.

---

### Step 7: Add Storybook Stories (Optional)

**Evaluate:** Is mocking straightforward? If yes, create stories. If no, skip.

These stories are a manual verification catalog - don't add `play` functions or assertions; just cover the states below and click through them by hand.

```typescript
// feature-name.stories.tsx
import { FeatureNamePresentation } from './feature-name';

function createMockProps(overrides?: Partial<UseFeatureNameHook>) {
  return {
    form: {
      /* mock form controls */
    },
    state: {
      /* mock state */
    },
    actions: {
      handleSave: action('handleSave'),
      // ...
    },
    data: { entities: [], isLoading: false },
    ...overrides,
  };
}

export const Default: Story = {
  args: createMockProps(),
};
```

**If mocking is too complex, skip Storybook and rely on domain + hook tests.**

---

## Migration Checklist

- [ ] Extract types to `types.ts`
- [ ] Identify all business logic in current code
- [ ] Extract business logic to `use-feature-name-domain.ts`
- [ ] Write domain function tests (HIGHEST PRIORITY)
- [ ] Extract form hook
- [ ] Extract state hook
- [ ] Extract mutations hook
- [ ] Extract actions hook (curry domain functions)
- [ ] Refactor main hook to orchestrator
- [ ] Split component into presentation and container (both exported in same file)
- [ ] Write hook unit tests
- [ ] Write integration test for main hook
- [ ] Add a component test (`.test.tsx`) if rendering logic is non-trivial
- [ ] Add interaction tests (`.component.spec.tsx`) for behavior a real browser is needed for
- [ ] Add screenshot tests (`.screenshot.spec.tsx`) for visual regressions worth pinning
- [ ] Evaluate if Storybook is worthwhile
- [ ] Create stories if straightforward
- [ ] Verify all tests pass
- [ ] Verify existing functionality unchanged
- [ ] Remove old unused code

---

## Common Pitfalls

### ❌ Don't: Keep business logic in actions

See [Step 2](#step-2-extract-business-logic-to-domain-functions) above for the full before/after example of extracting inline validation/transformation into domain functions.

---

### ❌ Don't: Expose mutation objects

```typescript
return {
  createMutation, // ❌ Exposing mutation object
  updateMutation,
};
```

### ✅ Do: Expose simple functions

```typescript
return {
  create: (data) => createMutation.mutateAsync(data), // ✅ Simple function
  isCreating: createMutation.isLoading,
};
```

---

### ❌ Don't: Mix concerns in one hook

```typescript
// ❌ Form + state + actions all in one hook
export function useFeatureName() {
  const { control } = useForm();
  const [isOpen, setIsOpen] = useState(false);
  const handleSave = () => {
    /* ... */
  };
  return { control, isOpen, setIsOpen, handleSave };
}
```

### ✅ Do: Separate concerns

```typescript
// ✅ Focused hooks
const form = useFeatureNameForm();
const state = useFeatureNameState();
const actions = useFeatureNameActions({ form, state });
```

---

See [Migration Strategy § Principles](#principles) above for the principles behind this checklist.

---

## When You're Stuck

1. **Can't identify business logic?**

   - Look for: validation, transformations, eligibility checks, calculations
   - Look in: event handlers, `useEffect`, inline conditions

2. **Hook dependencies getting complex?**

   - Review dependency flow: form → state → data → mutations → actions
   - Each hook should only depend on previous ones

3. **Mocking for Storybook too hard?**

   - Skip Storybook, focus on domain + hook tests
   - Container/presentation split still valuable for architecture

4. **Tests breaking during refactoring?**
   - Work in smaller steps
   - Test after each extraction
   - Keep existing behavior unchanged

---

## Success Criteria

✅ Domain logic extracted to pure functions
✅ Domain tests comprehensive and passing
✅ Each hook has single responsibility
✅ Main hook is "boring" orchestrator
✅ Component split into container/presentation
✅ Existing functionality unchanged
✅ All tests passing
