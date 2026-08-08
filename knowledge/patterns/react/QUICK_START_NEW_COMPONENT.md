# Quick Start: Creating a New Component

**Use this guide when building a new feature from scratch.**

**Note on Storybook:** Storybook is a **catalog for manual verification/QA** of components, not an automated test suite. Use stories to browse component states and click through them by hand. For automated coverage - including interaction and screenshot tests - see [Write Tests](#9-write-tests).

## File Structure You'll Create

```text
feature-name/
├── feature-name.tsx                    # Container and presentation (both exported)
├── feature-name.stories.tsx            # Storybook stories (optional)
├── use-feature-name.ts                 # Main orchestrator hook
├── use-feature-name-form.ts            # Form logic (if needed)
├── use-feature-name-state.ts           # UI state (if needed)
├── use-feature-name-mutations.ts       # API mutations (if needed)
├── use-feature-name-actions.ts         # User interaction handlers
├── use-feature-name-domain.ts          # Business logic (pure functions)
├── types.ts                            # All type definitions
└── __tests__/
    ├── feature-name-domain.test.ts     # Domain logic tests (HIGHEST PRIORITY)
    ├── use-feature-name-*.test.ts      # Hook tests
    └── use-feature-name.test.ts        # Integration test
```

---

## Step-by-Step Process

### 1. Define Types First (`types.ts`)

```typescript
// Props for the container component
export type FeatureNameProps = {
  onClose?: () => void;
  existingEntity?: ExistingEntityType;
};

// Form data structure
export type FeatureNameFormData = {
  field1: string;
  field2: number;
};

// Progressive hook props pattern - each hook gets props + previous results
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

// Hook return types - grouped by responsibility
export type FeatureNameFormControls = {
  control: Control<FeatureNameFormData>;
  handleSubmit: UseFormHandleSubmit<FeatureNameFormData>;
  errors: FieldErrors<FeatureNameFormData>;
  setValue: UseFormSetValue<FeatureNameFormData>;
};

// Public state (exposed to component)
export type FeatureNameState = {
  isDialogOpen: boolean;
  editingIndex: number | null;
  // ... other UI state
};

// Internal state (used by actions hook, includes setters)
export type FeatureNameStateInternal = FeatureNameState & {
  setIsDialogOpen: (open: boolean) => void;
  setEditingIndex: (index: number | null) => void;
  // ... other setters
};

export type FeatureNameActions = {
  openDialog: () => void;
  handleSave: () => void;
  // ... other actions
};

export type FeatureNameData = {
  entities: Entity[];
  isLoading: boolean;
};

export type FeatureNameMutations = {
  create: (data: CreateRequestType) => Promise<void>;
  isCreating: boolean;
  // ... other mutations
};

// Main hook interface
export type UseFeatureNameHook = {
  form: FeatureNameFormControls;
  state: FeatureNameState;
  actions: FeatureNameActions;
  data: FeatureNameData;
};
```

---

### 2. Write Domain Logic (`use-feature-name-domain.ts`)

**PURE FUNCTIONS ONLY** - No React hooks, no side effects, no API calls.

```typescript
/**
 * Validates form data according to business rules
 */
export function validateFormData(formData: FeatureNameFormData): {
  isValid: boolean;
  errors: string[];
} {
  const errors: string[] = [];

  if (!formData.field1 || formData.field1.trim() === '') {
    errors.push('Field 1 is required');
  }

  return { isValid: errors.length === 0, errors };
}

/**
 * Transforms form data to API request format
 */
export function transformFormDataToRequest(
  formData: FeatureNameFormData,
  entities: Entity[],
): CreateRequestType {
  return {
    name: formData.field1.trim(),
    value: formData.field2,
    entityIds: entities.map((e) => e.id),
  };
}

/**
 * Business rule: can this entity be deleted?
 */
export function canDeleteEntity(
  entity: Entity,
  relatedEntities: Entity[],
): { canDelete: boolean; reason?: string } {
  if (relatedEntities.length > 0) {
    return {
      canDelete: false,
      reason: 'Entity has related items',
    };
  }
  return { canDelete: true };
}
```

**Write tests immediately** (see Step 7).

---

### 3. Build Form Hook (`use-feature-name-form.ts`)

```typescript
// Props type defined in types.ts
// type UseFeatureNameFormProps = Pick<FeatureNameProps, 'existingEntity'>;

export function useFeatureNameForm(
  props: UseFeatureNameFormProps,
): FeatureNameFormControls {
  const getDefaultValues = useCallback((): FeatureNameFormData => {
    return {
      field1: props.existingEntity?.field1 ?? '',
      field2: props.existingEntity?.field2 ?? 0,
    };
  }, [props.existingEntity]);

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

---

### 4. Build State Hook (`use-feature-name-state.ts`)

```typescript
// Props type defined in types.ts
// type UseFeatureNameStateProps = FeatureNameProps & {
//   form: FeatureNameFormControls;
// };

export function useFeatureNameState(
  props: UseFeatureNameStateProps,
): FeatureNameStateInternal {
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);

  // Use domain functions for computed state
  // Watch form data via props.form
  const formData = props.form.watch();
  const computedValue = useMemo(() => {
    if (!formData.field1) return null;
    return computeDerivedValue(formData.field1);
  }, [formData.field1]);

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

---

### 5. Build Mutations Hook (`use-feature-name-mutations.ts`)

```typescript
// Props type defined in types.ts
// type UseFeatureNameMutationsProps = UseFeatureNameStateProps & {
//   state: FeatureNameState;
// };

// Return type defined in types.ts
// type FeatureNameMutations = { ... };

export function useFeatureNameMutations(
  props: UseFeatureNameMutationsProps,
): FeatureNameMutations {
  const showToast = useToast();
  const invalidateQueries = useEnvQueryInvalidator();

  const createMutation = useMutation({
    mutationFn: createEntity,
    onSuccess: () => {
      showToast('success', 'Created successfully');
      invalidateQueries(['getEntities']);
      props.onClose?.(); // From FeatureNameProps via progressive props
    },
    onError: () => {
      showToast('error', 'Failed to create');
    },
  });

  return {
    create: useCallback(
      (data: CreateRequestType) => createMutation.mutateAsync(data),
      [createMutation],
    ),
    // ... other mutations
    isCreating: createMutation.isLoading,
  };
}
```

---

### 6. Build Actions Hook (`use-feature-name-actions.ts`)

**Actions curry domain functions with hook state.**

```typescript
import {
  validateFormData,
  transformFormDataToRequest,
  canDeleteEntity,
} from './use-feature-name-domain';

// Props type defined in types.ts
// type UseFeatureNameActionsProps = UseFeatureNameMutationsProps & {
//   form: FeatureNameFormControls;
//   state: FeatureNameStateInternal;
//   data: FeatureNameData;
//   mutations: FeatureNameMutations;
// };

export function useFeatureNameActions(
  props: UseFeatureNameActionsProps,
): FeatureNameActions {
  const showToast = useToast();

  const handleSave = useCallback(async () => {
    const formData = props.form.control._formValues;

    // Use domain function for validation
    const validation = validateFormData(formData);
    if (!validation.isValid) {
      validation.errors.forEach((error) => showToast('error', error));
      return;
    }

    // Use domain function for transformation
    const requestData = transformFormDataToRequest(
      formData,
      props.data.entities,
    );

    await props.mutations.create(requestData);
  }, [props.form, props.data.entities, props.mutations, showToast]);

  const handleDelete = useCallback(
    async (id: string) => {
      const entity = props.data.entities.find((e) => e.id === id);
      if (!entity) return;

      // Use domain function for business rules
      const deleteCheck = canDeleteEntity(entity, []);
      if (!deleteCheck.canDelete) {
        showToast('error', deleteCheck.reason!);
        return;
      }

      await props.mutations.delete(id);
    },
    [props.data.entities, props.mutations, showToast],
  );

  return { handleSave, handleDelete };
}
```

---

### 7. Build Main Orchestrator (`use-feature-name.ts`)

**Should be "boring" - just wiring, no logic.**

```typescript
export function useFeatureName(props: FeatureNameProps): UseFeatureNameHook {
  // 1. Form controls (no dependencies)
  const form = useFeatureNameForm({
    existingEntity: props.existingEntity,
  });

  // 2. Local state (depends on form) - returns internal state with setters
  const stateInternal = useFeatureNameState({
    formData: form.watch(),
  });

  // 3. Query hooks directly (from consolidated files)
  const { data: entities = [], isLoading } = useEntities();

  // 4. Mutations
  const mutations = useFeatureNameMutations({
    onSuccess: props.onClose,
  });

  // 5. Actions (depends on everything above, receives internal state with setters)
  const actions = useFeatureNameActions({
    form,
    state: stateInternal,
    data: { entities, isLoading },
    mutations,
  });

  // Filter out setters before returning to component
  const { setIsDialogOpen, setEditingIndex, ...publicState } = stateInternal;

  return {
    form,
    state: publicState,
    actions,
    data: { entities, isLoading },
  };
}
```

---

### 8. Build Component File (`feature-name.tsx`)

**Contains both presentation and container components, both exported.**

```typescript
import { useFeatureName } from './use-feature-name';
import type { FeatureNameProps, UseFeatureNameHook } from './types';

/**
 * Pure presentation component - receives all data/callbacks as props
 * IMPORTANT: Must be exported for Storybook stories
 */
export function FeatureNamePresentation(
  props: UseFeatureNameHook,
): ReactElement {
  const { form, state, actions, data } = props;

  return (
    <Box>
      <form onSubmit={form.handleSubmit(actions.handleSave)}>
        <Controller
          name="field1"
          control={form.control}
          render={({ field }) => (
            <TextField
              {...field}
              error={!!form.errors.field1}
              helperText={form.errors.field1?.message}
            />
          )}
        />
        <Button type="submit">Save</Button>
      </form>
      {data.isLoading && <Spinner />}
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

---

### 9. Write Tests

**Priority order:**

1. **Domain tests** (`feature-name-domain.test.ts`) - HIGHEST PRIORITY

   ```typescript
   describe('validateFormData', () => {
     it('should validate correct data', () => {
       const result = validateFormData({ field1: 'test', field2: 10 });
       expect(result.isValid).toBe(true);
     });
   });
   ```

2. **Hook unit tests** - Test each hook with mocked dependencies
3. **Integration test** - Test main orchestrator with real sub-hooks
4. **Component test** (`feature-name.test.tsx`, optional) - RTL + `happy-dom`, for non-trivial rendering logic

   ```typescript
   render(<FeatureName title="My Title" />);
   expect(screen.getByText('My Title')).toBeTruthy();
   ```

5. **Interaction test** (`feature-name.component.spec.tsx`, optional) - real headless Chromium, for behavior `happy-dom` can't fake (hover, focus, real layout)

   ```typescript
   await renderComponent(<FeatureName />, VIEWPORTS.lg);
   await page.getByRole('button', { name: 'Add Item' }).click();
   await expect.element(page.getByRole('dialog')).toBeInTheDocument();
   ```

6. **Screenshot test** (`feature-name.screenshot.spec.tsx`, optional) - pixel-diff against a baseline PNG, for visual regressions structural assertions can't catch

   ```typescript
   await renderComponent(<FeatureName />, VIEWPORTS.sm);
   await expect
     .element(page.elementLocator(document.body))
     .toMatchScreenshot('default');
   ```

   Must run in the pinned Docker image (`pnpm run test:screenshot:docker`) - Chromium rasterizes differently per CPU architecture, so baselines captured natively on Apple Silicon won't match CI. Screenshot tests gate every PR in CI; a maintainer comments `/approve-screenshots` to accept new baselines.

See [COMPONENT_ORGANIZATION_CONVENTION.md § Testing Strategy](./COMPONENT_ORGANIZATION_CONVENTION.md#testing-strategy) for the full writeup, including a "which test type do I reach for" decision guide.

---

### 10. Add Storybook Stories (Optional)

**Only if mocking is straightforward. If complex, skip and focus on domain tests.**

These stories are a manual verification catalog - don't add `play` functions or assertions; just cover the states below and click through them by hand.

```typescript
import { FeatureNamePresentation } from './feature-name';
import type { UseFeatureNameHook } from './types';

function createMockProps(
  overrides?: Partial<UseFeatureNameHook>,
): UseFeatureNameHook {
  return {
    form: {
      control: {} as any,
      handleSubmit: (fn: any) => (e: any) => {
        e?.preventDefault();
        fn();
      },
      errors: {},
      setValue: action('setValue'),
    },
    state: {
      isDialogOpen: false,
      editingIndex: null,
    },
    actions: {
      handleSave: action('handleSave'),
      handleDelete: action('handleDelete'),
    },
    data: {
      entities: [],
      isLoading: false,
    },
    ...overrides,
  };
}

export const Default: Story = {
  args: createMockProps(),
};

export const Loading: Story = {
  args: createMockProps({
    data: { entities: [], isLoading: true },
  }),
};
```

---

## Checklist

- [ ] Create `types.ts` with all type definitions
- [ ] Create `use-feature-name-domain.ts` with pure functions
- [ ] Write domain tests immediately
- [ ] Create specialized hooks (form, state, mutations, actions)
- [ ] Create main orchestrator hook
- [ ] Create component file with both presentation and container (both exported)
- [ ] Add a component test (`.test.tsx`) if rendering logic is non-trivial
- [ ] Add interaction tests (`.component.spec.tsx`) for behavior a real browser is needed for
- [ ] Add screenshot tests (`.screenshot.spec.tsx`) for visual regressions worth pinning
- [ ] Evaluate if Storybook stories are worthwhile
- [ ] Create stories if mocking is straightforward
- [ ] Verify all tests pass

---

See [QUICK_REF_PATTERNS.md § Core Principles (The 10 Commandments)](./QUICK_REF_PATTERNS.md#core-principles-the-10-commandments) for the principles behind each step above.

---

## When to Skip Storybook

Skip stories if:

- Presentation uses complex pre-existing components that are hard to mock
- Mocking form controls becomes too cumbersome
- Component has minimal visual variations

**Always prioritize domain logic tests over Storybook stories.**
