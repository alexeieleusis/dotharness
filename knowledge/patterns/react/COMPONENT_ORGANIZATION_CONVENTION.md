# Component Organization Convention

This document defines the standard pattern for organizing complex components with custom hooks in this codebase.

## Goals

- Adhere to SOLID principles and software engineering best practices
- Make components easier to understand and maintain
- Establish conventions so every team member can contribute anywhere in the app
- Avoid hooks becoming too large and difficult to maintain
- Keep the main hook "boring" and easily readable
- Ensure linear, predictable data flow

## Testing Philosophy

This convention distinguishes two different activities that are often conflated:

- **Automated tests** (domain, hook, integration, component, interaction, screenshot) - covered in [Testing Strategy](#testing-strategy). These are what CI runs and what gives confidence the code is correct.
- **Storybook** - a **catalog for manual verification and visual QA** of components. It is not an automated test suite: use it to browse every component state and manually click through interactions during development and design review. `play` functions and Storybook-based interaction/screenshot testing are **not** part of this convention - automated interaction and pixel coverage lives in `.component.spec.tsx`/`.screenshot.spec.tsx` files instead (see [Testing Strategy](#testing-strategy)).

## Table of Contents

1. [File Structure & Naming](#file-structure--naming)
2. [Type Definitions](#type-definitions)
3. [Hook Organization Pattern](#hook-organization-pattern)
4. [Domain Logic Pattern](#domain-logic-pattern)
5. [Component Pattern: Container/Presentation Split](#component-pattern-containerpresentation-split)
6. [Storybook Stories Pattern](#storybook-stories-pattern)
7. [Dependency Flow](#dependency-flow)
8. [Testing Strategy](#testing-strategy)
9. [Best Practices](#best-practices)
10. [Creating a New Component from Scratch](#creating-a-new-component-from-scratch)
11. [Migration Strategy](#migration-strategy)
12. [Examples](#examples)

---

## File Structure & Naming

```text
feature-name/
├── feature-name.tsx                    # Container and presentation (both exported)
├── feature-name.stories.tsx            # Storybook stories (for presentation component)
├── use-feature-name.ts                 # Main orchestrator hook
├── use-feature-name-form.ts            # Form-specific logic
├── use-feature-name-state.ts           # Local state management
├── use-feature-name-mutations.ts       # API mutations
├── use-feature-name-actions.ts         # User interaction handlers
├── use-feature-name-domain.ts          # Domain-specific logic (pure functions)
├── use-feature-name-render.tsx         # Render prop helpers (optional)
├── types.ts                            # Shared types for the feature
└── __tests__/                          # Tests
    ├── feature-name-domain.test.ts     # Business logic tests (pure functions)
    ├── use-feature-name-form.test.ts   # Form hook tests
    ├── use-feature-name-state.test.ts  # State hook tests
    ├── use-feature-name-actions.test.ts # Actions hook tests
    ├── use-feature-name.test.ts        # Integration tests
    ├── feature-name.test.tsx           # Component test (happy-dom), optional
    ├── feature-name.component.spec.tsx # Interaction test (real browser), optional
    └── feature-name.screenshot.spec.tsx # Screenshot test (real browser), optional
```

**Note:** Query hooks (React Query) are managed in consolidated files (e.g., `src/hooks/queries.ts`), not feature-specific files.

### Naming Convention

- Use `use-*` prefix for all hooks
- Suffix indicates responsibility: `-form`, `-state`, `-mutations`, `-actions`, `-domain`, `-render`
- Keep presentational component name simple (no `-component` suffix)
- Query hooks live in consolidated files, not feature-specific files

---

## Type Definitions

All types should be defined in `types.ts` within the feature directory.

### Props Types

```typescript
// Always use `type`, not `interface` (per codebase convention)
export type FeatureNameProps = {
  onClose?: () => void;
  existingEntity?: ExistingEntityType;
  initialData?: Partial<DataType>;
};
```

### Form Data Structure

```typescript
export type FeatureNameFormData = {
  field1: string;
  field2: number;
  // ... form fields
};
```

### Progressive Hook Props Pattern

**IMPORTANT:** Each hook's props type progressively includes the original props plus results from previous hooks in the dependency chain. This creates a clear, type-safe dependency flow.

```typescript
// Form hook props - just the original props (or subset)
export type UseFeatureNameFormProps = Pick<
  FeatureNameProps,
  'existingEntity' | 'initialData'
>;

// State hook props - original props + form hook result
export type UseFeatureNameStateProps = FeatureNameProps & {
  form: FeatureNameFormControls;
};

// Mutations hook props - original props + state props + state result
export type UseFeatureNameMutationsProps = UseFeatureNameStateProps & {
  state: FeatureNameState;
};

// Actions hook props - all previous props + all previous results
export type UseFeatureNameActionsProps = UseFeatureNameMutationsProps & {
  form: FeatureNameFormControls;
  state: FeatureNameStateInternal; // Internal state with setters - actions call them
  data: FeatureNameData;
  mutations: FeatureNameMutations;
};
```

**Alternative Explicit Pattern** (when you don't need all original props at each level):

```typescript
// Form hook - only what it needs from props
export type UseFeatureNameFormProps = {
  existingEntity?: ExistingEntityType;
  initialData?: Partial<DataType>;
};

// State hook - only what it needs (form results)
export type UseFeatureNameStateProps = {
  formData: Partial<FeatureNameFormData>;
};

// Mutations hook - only what it needs from props
export type UseFeatureNameMutationsProps = {
  onSuccess?: () => void;
};

// Actions hook - everything it needs to orchestrate (receives internal state with setters)
export type UseFeatureNameActionsProps = {
  form: FeatureNameFormControls;
  state: FeatureNameStateInternal; // Internal state with setters
  data: FeatureNameData;
  mutations: FeatureNameMutations;
  onClose?: () => void;
};
```

**Choose the pattern that fits your needs:**

- **Progressive intersection** (`Props & { ... }`): When hooks need access to original props throughout the chain
- **Explicit minimal**: When each hook only needs specific dependencies (cleaner, more maintainable, recommended)

### Hook Return Types

Organize by responsibility:

```typescript
// Form controls and methods
export type FeatureNameFormControls = {
  control: Control<FeatureNameFormData>;
  handleSubmit: UseFormHandleSubmit<FeatureNameFormData>;
  errors: FieldErrors<FeatureNameFormData>;
  setValue: UseFormSetValue<FeatureNameFormData>;
  // Field arrays if needed
  items: FieldArrayWithId<FeatureNameFormData, 'items', 'id'>[];
  addItem: () => void;
  removeItem: (index: number) => void;
  updateItem: (index: number, data: ItemData) => void;
};

// Public state (exposed to component)
export type FeatureNameState = {
  isDialogOpen: boolean;
  editingIndex: number | null;
  selectedId: string | null;
  // Derived/computed state
  computedValue: string | null;
};

// Internal state (used by actions hook, includes setters)
export type FeatureNameStateInternal = FeatureNameState & {
  setIsDialogOpen: (open: boolean) => void;
  setEditingIndex: (index: number | null) => void;
  setSelectedId: (id: string | null) => void;
};

// User-triggered actions (no mutation objects exposed)
export type FeatureNameActions = {
  openDialog: () => void;
  closeDialog: () => void;
  handleEdit: (index: number) => void;
  handleDelete: (id: string) => void;
  handleSave: () => void;
};

// Data fetched or derived
export type FeatureNameData = {
  entities: Entity[];
  scope: EventScope | undefined;
  isLoading: boolean;
};

// Main hook interface - composed from smaller pieces
export type UseFeatureNameHook = {
  form: FeatureNameFormControls;
  state: FeatureNameState;
  actions: FeatureNameActions;
  data: FeatureNameData;
};
```

### Key Principles

- **Never expose mutations directly** - wrap them in action callbacks
- **Group by responsibility** - form, state, actions, data
- **Separate internal/public state** - internal includes setters, public doesn't
- **Flat is better than nested** - but namespacing prevents collision
- **Avoid primitives at top level** - group related primitives

---

## Hook Organization Pattern

### Main Orchestrator Hook

**File:** `use-feature-name.ts`

The main hook should be "boring" and highly readable - just composition and wiring, no business logic.

#### Standard Pattern

```typescript
/**
 * Main orchestrator hook - delegates to specialized hooks
 * Should be "boring" and highly readable
 */
export function useFeatureName(props: FeatureNameProps): UseFeatureNameHook {
  // 1. Form controls (no dependencies)
  const form = useFeatureNameForm({
    existingEntity: props.existingEntity,
    initialData: props.initialData,
  });

  // 2. Local state (depends on form) - returns internal state with setters
  const stateInternal = useFeatureNameState({
    formData: form.watch(),
  });

  // 3. Data fetching - call query hooks directly from consolidated query files
  const { data: entities = [], isLoading: isLoadingEntities } = useEntities();
  const { data: scopeData } = useEventScopeFromId(stateInternal.selectedId);

  // 4. Mutations (depends on props)
  const mutations = useFeatureNameMutations({
    onSuccess: props.onClose,
  });

  // 5. Actions (depends on everything above, receives internal state with setters)
  const actions = useFeatureNameActions({
    form,
    state: stateInternal,
    data: { entities, scopeData, isLoading: isLoadingEntities },
    mutations,
    onClose: props.onClose,
  });

  // Filter out setters from state before returning
  const { setIsDialogOpen, setEditingIndex, setSelectedId, ...publicState } =
    stateInternal;

  return {
    form,
    state: publicState,
    actions,
    data: { entities, scopeData, isLoading: isLoadingEntities },
  };
}
```

#### Alternative: Generic Orchestrator Utility (Optional)

A generic `createHookOrchestrator<TProps, TForm, TState, TMutations, TActions>(config)` utility can encapsulate the build-form → build-state → build-mutations → build-actions wiring behind builder functions, enforcing the dependency flow at the type level instead of by convention.

**Recommendation:** Use the standard pattern by default. Only reach for this if you have many complex features following this exact shape and want the dependency flow enforced at compile time - it trades explicitness for reduced boilerplate.

**Key Principles:**

- **Linear dependency flow**: Each section can only depend on previous sections
- **Single Responsibility**: Each hook does one thing well
- **Explicit dependencies**: Props clearly show what each hook needs
- **No business logic**: Just composition and wiring

---

### Form Hook

**File:** `use-feature-name-form.ts`

Handles all form-related logic including validation and field arrays.

```typescript
type UseFeatureNameFormProps = {
  existingEntity?: ExistingEntityType;
  initialData?: Partial<DataType>;
};

export function useFeatureNameForm(
  props: UseFeatureNameFormProps,
): FeatureNameFormControls {
  const getDefaultValues = useCallback((): FeatureNameFormData => {
    return {
      field1: props.existingEntity?.field1 ?? '',
      field2: props.initialData?.field2 ?? 0,
    };
  }, [props.existingEntity, props.initialData]);

  const {
    control,
    handleSubmit,
    formState: { errors },
    setValue,
  } = useOscForm<FeatureNameFormData>({
    resolver: yupResolver(validationSchema),
    defaultValues: getDefaultValues(),
  });

  // Field arrays if needed
  const {
    fields: items,
    append,
    remove,
    update,
  } = useFieldArray({
    control,
    name: 'items',
  });

  return {
    control,
    handleSubmit,
    errors,
    setValue,
    items,
    // Include field array methods with meaningful names
    addItem: useCallback(() => append(defaultItem), [append]),
    removeItem: useCallback((index: number) => remove(index), [remove]),
    updateItem: useCallback(
      (index: number, data: ItemData) => update(index, data),
      [update],
    ),
  };
}
```

**Key Principles:**

- **No side effects**: Pure form state management
- **Include validation**: Schema defined in same file
- **Field array helpers**: Wrap append/remove/update with meaningful names
- **Memoize callbacks**: Use `useCallback` for all methods

---

### State Hook

**File:** `use-feature-name-state.ts`

Manages UI state and computed/derived values.

```typescript
type UseFeatureNameStateProps = {
  formData: Partial<FeatureNameFormData>;
};

export function useFeatureNameState(
  props: UseFeatureNameStateProps,
): FeatureNameStateInternal {
  // UI state
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Computed/derived state
  const computedValue = useMemo(() => {
    if (!props.formData.field1) return null;
    return transformFormData(props.formData.field1);
  }, [props.formData.field1]);

  // State update effects
  useEffect(() => {
    if (props.formData.field2 > 0) {
      setSelectedId(String(props.formData.field2));
    }
  }, [props.formData.field2]);

  return {
    // Public state values
    isDialogOpen,
    editingIndex,
    selectedId,
    computedValue,
    // Setters (filtered out by orchestrator before passing to component)
    setIsDialogOpen,
    setEditingIndex,
    setSelectedId,
  };
}
```

**Key Principles:**

- **UI state only**: Modal open/closed, editing indices, selections
- **Computed state**: Derive from form data or other state
- **Returns internal type**: Includes both state values and setters
- **Setters filtered by orchestrator**: Main hook removes setters before returning to component
- **No business logic**: Just state management

---

### Mutations Hook

**File:** `use-feature-name-mutations.ts`

Wraps React Query mutations and hides mutation objects from the component.

```typescript
type UseFeatureNameMutationsProps = {
  onSuccess?: () => void;
};

type FeatureNameMutations = {
  create: (data: CreateRequestType) => Promise<void>;
  update: (data: UpdateRequestType) => Promise<void>;
  delete: (id: string) => Promise<void>;
  isCreating: boolean;
  isUpdating: boolean;
  isDeleting: boolean;
};

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
      props.onSuccess?.();
    },
    onError: () => {
      showToast('error', 'Failed to create');
    },
  });

  const updateMutation = useMutation({
    mutationFn: updateEntity,
    onSuccess: () => {
      showToast('success', 'Updated successfully');
      invalidateQueries(['getEntities']);
      props.onSuccess?.();
    },
    onError: () => {
      showToast('error', 'Failed to update');
    },
  });

  const deleteMutation = useMutation({
    mutationFn: deleteEntity,
    onSuccess: () => {
      showToast('success', 'Deleted successfully');
      invalidateQueries(['getEntities']);
      props.onSuccess?.();
    },
    onError: () => {
      showToast('error', 'Failed to delete');
    },
  });

  return {
    create: useCallback(
      (data: CreateRequestType) => createMutation.mutateAsync(data),
      [createMutation],
    ),
    update: useCallback(
      (data: UpdateRequestType) => updateMutation.mutateAsync(data),
      [updateMutation],
    ),
    delete: useCallback(
      (id: string) => deleteMutation.mutateAsync(id),
      [deleteMutation],
    ),
    isCreating: createMutation.isLoading,
    isUpdating: updateMutation.isLoading,
    isDeleting: deleteMutation.isLoading,
  };
}
```

**Key Principles:**

- **Hide React Query mutations**: Expose simple async functions
- **Include loading states**: Extract `isLoading` flags with meaningful names
- **Handle success/error**: Toast notifications and query invalidation
- **Return promises**: Use `mutateAsync` for better composition

---

### Actions Hook

**File:** `use-feature-name-actions.ts`

Orchestrates user interactions by currying domain functions with hook state. Business logic should live in domain functions, not here.

```typescript
import {
  validateFormData,
  transformFormDataToRequest,
  canDeleteEntity,
  mergeEntityWithFormData,
} from './use-feature-name-domain';

type UseFeatureNameActionsProps = {
  form: FeatureNameFormControls;
  state: FeatureNameStateInternal; // Internal state with setters
  data: FeatureNameData;
  mutations: FeatureNameMutations;
  onClose?: () => void;
};

export function useFeatureNameActions(
  props: UseFeatureNameActionsProps,
): FeatureNameActions {
  const showToast = useToast();

  const openDialog = useCallback(() => {
    props.state.setIsDialogOpen(true);
  }, [props.state.setIsDialogOpen]);

  const closeDialog = useCallback(() => {
    props.state.setIsDialogOpen(false);
    props.state.setEditingIndex(null);
  }, [props.state.setIsDialogOpen, props.state.setEditingIndex]);

  const handleEdit = useCallback(
    (index: number) => {
      props.state.setEditingIndex(index);
      props.state.setIsDialogOpen(true);
    },
    [props.state.setEditingIndex, props.state.setIsDialogOpen],
  );

  const handleDelete = useCallback(
    async (id: string) => {
      const entity = props.data.entities.find((e) => e.id === id);
      if (!entity) return;

      // Use domain function for business rule validation
      const deleteCheck = canDeleteEntity(entity, props.data.relatedEntities);
      if (!deleteCheck.canDelete) {
        showToast('error', deleteCheck.reason!);
        return;
      }

      if (!confirm('Are you sure?')) return;
      await props.mutations.delete(id);
    },
    [
      props.data.entities,
      props.data.relatedEntities,
      props.mutations,
      showToast,
    ],
  );

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

    // Determine create vs update and use appropriate mutation
    if (props.state.editingIndex !== null) {
      const updateData = mergeEntityWithFormData(
        props.data.entities[props.state.editingIndex],
        formData,
      );
      await props.mutations.update(updateData);
    } else {
      await props.mutations.create(requestData);
    }

    closeDialog();
  }, [
    props.form.control,
    props.state.editingIndex,
    props.data.entities,
    props.mutations,
    showToast,
    closeDialog,
  ]);

  return {
    openDialog,
    closeDialog,
    handleEdit,
    handleDelete,
    handleSave,
  };
}
```

**Key Principles:**

- **Orchestration only**: Wire together domain functions with hook state
- **No business logic**: All domain logic lives in domain functions
- **Curry domain functions**: Pass hook state/data to pure functions
- **All user interactions**: Every button click, form submit, etc.
- **Coordinate mutations**: Call mutation functions (not mutation objects)
- **Memoize everything**: All callbacks wrapped in `useCallback`

**Note on Data Fetching:**

- Query hooks (React Query) are managed in consolidated query files (e.g., `src/hooks/queries.ts`)
- Call query hooks directly in the main orchestrator hook or in the actions hook as needed - not in the state hook, since [Dependency Flow](#dependency-flow) places query hooks after state
- Keep query hooks simple - complex data transformations should use domain functions

---

## Domain Logic Pattern

**File:** `use-feature-name-domain.ts`

This file contains all business logic as pure functions. These functions should be side-effect free and easily testable in isolation.

### Purpose

- **Centralize business logic**: All domain rules and transformations in one place
- **Pure functions**: No React hooks, no side effects, easily testable
- **Reusability**: Can be used by multiple hooks (actions, mutations, state)
- **Testability**: Pure functions are trivial to unit test

### Structure

```typescript
/**
 * Domain logic for feature-name
 * All functions should be pure and side-effect free
 */

// Type definitions for domain operations
type TransformInput = {
  formData: FeatureNameFormData;
  entities: Entity[];
};

type TransformOutput = {
  requestData: CreateRequestType;
  validationErrors: string[];
};

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

  if (formData.field2 < 0) {
    errors.push('Field 2 must be positive');
  }

  return {
    isValid: errors.length === 0,
    errors,
  };
}

/**
 * Transforms form data to API request format
 */
export function transformFormDataToRequest(
  formData: FeatureNameFormData,
  entities: Entity[],
): CreateRequestType {
  // Pure transformation logic
  return {
    name: formData.field1.trim(),
    value: formData.field2,
    entityIds: entities.map((e) => e.id),
  };
}

/**
 * Calculates derived state from form data
 */
export function computeDerivedValue(field1: string): string | null {
  if (!field1) return null;

  // Business logic for computation
  return field1.toUpperCase().replace(/\s+/g, '_');
}

/**
 * Determines if entity can be deleted based on business rules
 */
export function canDeleteEntity(
  entity: Entity,
  relatedEntities: Entity[],
): { canDelete: boolean; reason?: string } {
  if (relatedEntities.length > 0) {
    return {
      canDelete: false,
      reason: 'Entity has related items that must be deleted first',
    };
  }

  if (entity.status === 'active') {
    return {
      canDelete: false,
      reason: 'Active entities cannot be deleted',
    };
  }

  return { canDelete: true };
}

/**
 * Merges existing entity with form updates
 */
export function mergeEntityWithFormData(
  existingEntity: ExistingEntityType,
  formData: FeatureNameFormData,
): UpdateRequestType {
  return {
    id: existingEntity.id,
    name: formData.field1,
    value: formData.field2,
    // Keep existing fields that aren't being updated
    createdAt: existingEntity.createdAt,
    updatedAt: new Date().toISOString(),
  };
}
```

### Usage in Actions Hook

Actions hook curries domain functions with hook state - see the [Actions Hook](#actions-hook) section above for the full `handleSave`/`handleDelete` example (validate → transform → mutate).

### Usage in State Hook

State hook can use domain functions for derived state:

```typescript
export function useFeatureNameState(
  props: UseFeatureNameStateProps,
): FeatureNameStateInternal {
  const [isDialogOpen, setIsDialogOpen] = useState(false);

  // Use domain function for computed state
  const computedValue = useMemo(() => {
    if (!props.formData.field1) return null;
    return computeDerivedValue(props.formData.field1);
  }, [props.formData.field1]);

  return {
    isDialogOpen,
    computedValue,
    setIsDialogOpen, // Setter included in internal type
  };
}
```

### Usage in Mutations Hook

Mutations hook can use domain functions for data transformation:

```typescript
export function useFeatureNameMutations(
  props: UseFeatureNameMutationsProps,
): FeatureNameMutations {
  const createMutation = useMutation({
    mutationFn: (formData: FeatureNameFormData) => {
      // Use domain function to transform before API call
      const requestData = transformFormDataToRequest(formData, []);
      return createEntity(requestData);
    },
    onSuccess: () => {
      showToast('success', 'Created successfully');
      props.onSuccess?.();
    },
  });

  return {
    create: useCallback(
      (formData: FeatureNameFormData) => createMutation.mutateAsync(formData),
      [createMutation],
    ),
    isCreating: createMutation.isLoading,
  };
}
```

### Key Principles

- **Pure functions only**: No side effects, no React hooks, no API calls
- **Single Responsibility**: Each function does one thing well
- **Testability**: Every function can be unit tested in isolation
- **Type safety**: Strong input/output types for all functions
- **Business logic only**: No UI concerns, no framework concerns
- **Composability**: Small functions that can be combined
- **Documentation**: Clear JSDoc comments explaining business rules

### Testing Domain Functions

Domain functions are trivially testable since they're pure - see [Domain Logic Tests](#1-domain-logic-tests-highest-priority) in Testing Strategy for a full worked example.

---

## Component Pattern: Container/Presentation Split

To enable easy manual verification in Storybook, we separate the container and presentation components, but they live in the same file for simplicity. Both are exported so Storybook can import the presentation component directly as a catalog entry.

### Component File Structure

**File:** `feature-name.tsx`

```typescript
import { useFeatureName } from './use-feature-name';
import type { FeatureNameProps, UseFeatureNameHook } from './types';

/**
 * Pure presentation component - receives all data and callbacks as props
 * This component is cataloged in Storybook for manual verification
 *
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

        <Button onClick={actions.openDialog}>
          Add Item
        </Button>

        {data.isLoading && <Spinner />}
      </form>

      <Dialog open={state.isDialogOpen} onClose={actions.closeDialog}>
        {/* Dialog content */}
      </Dialog>
    </Box>
  );
}

/**
 * Container component - connects hook to presentation
 * This is the default export used in the application
 */
export function FeatureName(props: FeatureNameProps): ReactElement {
  const hookResult = useFeatureName(props);
  return <FeatureNamePresentation {...hookResult} />;
}
```

**Key Principles:**

### Presentation Component

- **No hooks**: Pure component, all data comes from props
- **Only JSX and styling**: No logic whatsoever
- **Easy to verify manually**: Can pass mock props directly in Storybook
- **Type is hook return type**: Props type is `UseFeatureNameHook`
- **Must be exported**: Required for Storybook stories to import

### Container Component

- **One hook call**: Only calls `useFeatureName()`
- **Pass through**: Spreads hook result to presentation component
- **No JSX complexity**: Just the connection between hook and presentation
- **This is what gets imported**: Used in actual application

---

### Why This Split?

#### Benefits

1. **Storybook Catalog**

   - Can verify the presentation component manually with mock props
   - No need to mock React Query, hooks, or API calls
   - Just pass plain objects for `form`, `state`, `actions`, `data`

2. **True Separation**

   - Container handles "how to get data" (hook logic)
   - Presentation handles "how to display data" (rendering)
   - Clean boundary between logic and UI

3. **Easier Mocking**

   - In stories, create mock objects that match the hook return type
   - No need to mock entire hook implementation
   - Toast feedback works naturally with mock actions

4. **Better Manual Verification**
   - Can review the presentation component in complete isolation
   - Every UI state can be browsed without triggering hook logic
   - Automated visual regression (screenshot) testing is out of scope for this document and will be covered separately

#### Example: With and Without Split

```typescript
// ❌ Without split - hard to verify manually in Storybook
export function FeatureName(props: FeatureNameProps): ReactElement {
  const { form, state, actions, data } = useFeatureName(props);
  // Must mock entire hook, queries, mutations...
  return <JSX />;
}

// ✅ With split - see the full FeatureNamePresentation/FeatureName
// pair under Component File Structure above
```

---

## Storybook Stories Pattern

**Purpose:** Storybook is a **catalog for manual verification and visual QA** of components. It lets developers, designers, and reviewers browse every state of a component and manually interact with it in isolation. It is **not** an automated test suite - `play` functions, `@storybook/jest` assertions, and screenshot/visual-regression tooling are intentionally out of scope here. For automated coverage of interactions and pixels, see [Interaction Tests](#5-interaction-tests-componentspectsx-real-browser) and [Screenshot Tests](#6-screenshot-tests-screenshotspectsx-visual-regression) in [Testing Strategy](#testing-strategy).

**File:** `feature-name.stories.tsx`

Stories catalog the **presentation component**, not the container, so it can be reviewed manually in isolation. This makes cataloging trivial since we just pass mock props.

### Basic Structure

```typescript
import type { Meta, StoryObj } from '@storybook/react';
import { action } from '@storybook/addon-actions';
import { FeatureNamePresentation } from './feature-name';
import type { UseFeatureNameHook } from './types';

const meta: Meta<typeof FeatureNamePresentation> = {
  title: 'Features/FeatureName',
  component: FeatureNamePresentation,
  parameters: {
    layout: 'centered',
  },
  tags: ['autodocs'],
};

export default meta;
type Story = StoryObj<typeof FeatureNamePresentation>;

// Helper to create mock props
function createMockProps(
  overrides?: Partial<UseFeatureNameHook>,
): UseFeatureNameHook {
  return {
    form: {
      control: {} as any, // Mock form control
      handleSubmit: (fn: any) => (e: any) => {
        e?.preventDefault();
        fn();
      },
      errors: {},
      setValue: action('setValue'),
      items: [],
      addItem: action('addItem'),
      removeItem: action('removeItem'),
      updateItem: action('updateItem'),
    },
    state: {
      isDialogOpen: false,
      editingIndex: null,
      selectedId: null,
      computedValue: null,
    },
    actions: {
      openDialog: action('openDialog'),
      closeDialog: action('closeDialog'),
      handleEdit: action('handleEdit'),
      handleDelete: action('handleDelete'),
      handleSave: action('handleSave'),
    },
    data: {
      entities: [],
      scope: undefined,
      isLoading: false,
    },
    ...overrides,
  };
}

// Default story
export const Default: Story = {
  args: createMockProps(),
};

// With data
export const WithData: Story = {
  args: createMockProps({
    data: {
      entities: [
        { id: '1', name: 'Entity 1' },
        { id: '2', name: 'Entity 2' },
      ],
      scope: { id: 'scope-1', name: 'Scope 1' } as any,
      isLoading: false,
    },
  }),
};

// Loading state
export const Loading: Story = {
  args: createMockProps({
    data: {
      entities: [],
      scope: undefined,
      isLoading: true,
    },
  }),
};

// Dialog open
export const DialogOpen: Story = {
  args: createMockProps({
    state: {
      isDialogOpen: true,
      editingIndex: null,
      selectedId: null,
      computedValue: null,
    },
  }),
};

// With validation errors
const baseMockProps = createMockProps();
export const WithValidationErrors: Story = {
  args: createMockProps({
    form: {
      ...baseMockProps.form,
      errors: {
        field1: { message: 'Field 1 is required' },
        field2: { message: 'Field 2 must be positive' },
      },
    },
  }),
};
```

### Manual Interaction Verification with Actions

Actions are already set up in the mock props. During manual verification, open the story in Storybook's UI and interact with it by hand:

- Click "Add Item" and confirm `openDialog` appears in the Actions panel
- Click "Save" and confirm `handleSave` appears in the Actions panel

This is a manual check performed by a person in the Storybook UI - no `play` function or assertion library is involved. For automated interaction coverage, write a [`.component.spec.tsx` interaction test](#5-interaction-tests-componentspectsx-real-browser) instead.

### Visual Feedback with Toast Messages

For better manual verification, wrap action callbacks with toast feedback:

```typescript
import { useToast } from '@osc/hooks/use-toast';
import { action } from '@storybook/addon-actions';

// Helper to create actions with toast feedback
function createActionsWithToast(): UseFeatureNameHook['actions'] {
  const showToast = useToast();

  return {
    openDialog: () => {
      showToast('info', 'Dialog opened');
      action('openDialog')();
    },
    closeDialog: () => {
      showToast('info', 'Dialog closed');
      action('closeDialog')();
    },
    handleEdit: (index: number) => {
      showToast('info', `Editing item ${index}`);
      action('handleEdit')(index);
    },
    handleDelete: (id: string) => {
      showToast('warning', `Deleted: ${id}`);
      action('handleDelete')(id);
    },
    handleSave: () => {
      showToast('success', 'Saved successfully');
      action('handleSave')();
    },
  };
}

export const WithToastFeedback: Story = {
  args: createMockProps({
    actions: createActionsWithToast(),
  }),
};
```

**Benefits:**

- Click any button and see a toast message immediately
- No need to check Actions panel - visual feedback in the component
- Easy to verify interactions during manual testing
- Better for design reviews and demonstrations

### Testing Different States

Create stories for different UI states by simply changing the mock props. (`Loading` and `WithValidationErrors` are already shown under Basic Structure above - only the new ones follow.)

```typescript
// Empty state
export const Empty: Story = {
  args: createMockProps({
    data: {
      entities: [],
      scope: undefined,
      isLoading: false,
    },
  }),
};

// Error state - show in UI
export const WithError: Story = {
  args: createMockProps({
    data: {
      entities: [],
      scope: undefined,
      isLoading: false,
      error: 'Failed to load entities',
    },
  }),
};

// Editing mode
export const EditingItem: Story = {
  args: createMockProps({
    state: {
      isDialogOpen: true,
      editingIndex: 2,
      selectedId: '3',
      computedValue: 'COMPUTED_VALUE',
    },
    data: {
      entities: [
        { id: '1', name: 'Entity 1' },
        { id: '2', name: 'Entity 2' },
        { id: '3', name: 'Entity 3' },
      ],
      scope: undefined,
      isLoading: false,
    },
  }),
};
```

**Key Point:** No MSW, no mocking libraries - just plain objects!

### Cataloging Complex Workflows

For a multi-step workflow, create a story with the mock props needed for that scenario, then walk through it by hand in Storybook's UI (fill the form, open the dialog, save) to visually confirm it behaves correctly:

```typescript
export const CompleteWorkflow: Story = {
  args: createMockProps({
    // Mock props representing the starting point of the workflow
  }),
};
```

**Note:** Automating this kind of workflow (`play` functions, `@storybook/testing-library`, `@storybook/jest`) is out of scope for Storybook in this convention. Write a [`.component.spec.tsx` interaction test](#5-interaction-tests-componentspectsx-real-browser) for automated coverage of a multi-step workflow like this.

### Key Principles

- **Catalog the presentation component, not the container** - Stories use the `-presentation.tsx` component
- **No hooks in stories** - Just pass mock props matching `UseFeatureNameHook` type
- **Create mock helper** - `createMockProps()` function with overrides for easy story creation
- **Use actions for manual verification** - Storybook actions are automatically logged in the Actions panel for a person to check
- **Add toast feedback for better UX** - Wrap actions with toast calls for visual confirmation
- **Cover all states** - Empty, loading, error, validation errors, editing, etc.
- **No MSW needed** - Since we catalog the presentation component, no need to mock API calls
- **Document prop variations** - Create stories for each significant prop combination
- **Type safety** - Mock props must match `UseFeatureNameHook` type exactly
- **Use judgment on effort** - If complex pre-existing components make mocking difficult, focus on domain logic tests instead
- **Comprehensive domain tests are the priority** - Storybook stories are nice-to-have for manual verification, but automated domain logic tests are essential
- **No automated testing here** - `play` functions and interaction/screenshot assertions are not part of Storybook in this convention; see [Interaction Tests](#5-interaction-tests-componentspectsx-real-browser) and [Screenshot Tests](#6-screenshot-tests-screenshotspectsx-visual-regression) for that coverage

### Benefits

- **Visual documentation**: Team can see all component states
- **Manual verification**: Developers can interact with components in isolation and confirm behavior by hand
- **Design review**: Easy for designers to review UI states
- **Regression detection**: Visual changes are immediately apparent to a human reviewer
- **Reduced mocking complexity**: Since component has no logic, mocking is simpler (though complex pre-existing components may still require effort)

### When Storybook Stories Are Optional

Storybook stories may not be worth the effort if:

- Presentation component uses complex pre-existing components that are difficult to mock
- Component has minimal visual variations
- Mocking form controls or data structures becomes too cumbersome

**In these cases:**

- Focus on comprehensive domain logic tests (highest priority)
- Focus on hook unit tests (second priority)
- Skip or minimize Storybook stories
- The container/presentation split is still valuable for architecture clarity

---

## Dependency Flow

```text
Domain Functions (pure, no dependencies)
  ↓
Props
  ↓
useFeatureNameForm (no dependencies)
  ↓
useFeatureNameState (depends on: form data via watch)
  ↓
Query Hooks (from consolidated files, depends on: state.selectedId)
  ↓
useFeatureNameMutations (depends on: props.onSuccess, uses domain functions)
  ↓
useFeatureNameActions (depends on: form, state, data, mutations, uses domain functions)
  ↓
useFeatureName (orchestrates all)
  ↓
Component (consumes hook)
```

### Rules

- **Domain functions are pure and can be used by any hook** - no React dependencies
- **Query hooks live in consolidated files** - not feature-specific
- **Each hook can only depend on hooks above it in the flow**
- **No circular dependencies**
- **Linear, predictable flow**
- **Explicit prop dependencies** make the flow clear
- **Business logic lives in domain functions** - hooks orchestrate and curry them

---

## Testing Strategy

Testing should follow the same separation of concerns as the code organization. This section covers **automated tests** (domain, hook, integration, component, interaction, and screenshot). Storybook is a separate, manual-verification catalog - see [Storybook Stories Pattern](#storybook-stories-pattern) - and is not part of the automated test suite.

### Test File Structure

```text
feature-name/
└── __tests__/
    ├── feature-name-domain.test.ts       # Pure function tests (highest priority)
    ├── use-feature-name-form.test.ts     # Form hook tests
    ├── use-feature-name-state.test.ts    # State hook tests
    ├── use-feature-name-actions.test.ts  # Actions hook tests
    ├── use-feature-name.test.ts          # Integration tests
    ├── feature-name.test.tsx             # Component test (happy-dom) - optional
    ├── feature-name.component.spec.tsx   # Interaction test (real browser) - optional
    └── feature-name.screenshot.spec.tsx  # Screenshot test (real browser) - optional
```

The `.test.ts`/`.test.tsx` suffix runs in Vitest's `unit` project (`happy-dom`, no real browser). `.component.spec.tsx` and `.screenshot.spec.tsx` are separate Vitest projects that boot a real, headless Chromium via `@vitest/browser-playwright` - see [Interaction Tests](#5-interaction-tests-componentspectsx-real-browser) and [Screenshot Tests](#6-screenshot-tests-screenshotspectsx-visual-regression) below.

### Choosing the Right Test Type

Work down this list - reach for the cheapest test that can actually prove the behavior:

1. **Pure calculation or business rule?** → Domain test
2. **A single hook's behavior in isolation?** → Hook unit test
3. **Multiple hooks wired together?** → Integration test
4. **Non-trivial conditional rendering, and `happy-dom` is enough** (no hover, real layout, or focus order needed)? → Component test (`.test.tsx`)
5. **User-driven interaction that depends on a real browser** (hover, focus order, real CSS layout/animation, portals, keyboard navigation)? → Interaction test (`.component.spec.tsx`)
6. **The thing you're actually verifying is pixels themselves** (icon rendering, color, spacing, a complex visual layout that's tedious or impossible to assert structurally)? → Screenshot test (`.screenshot.spec.tsx`)
7. **Just want to browse every state, or have a designer/reviewer eyeball it by hand?** → Storybook story (manual catalog only - see [Storybook Stories Pattern](#storybook-stories-pattern))

### 1. Domain Logic Tests (Highest Priority)

Test pure functions in isolation - these are the most important tests since they contain business logic.

```typescript
// feature-name-domain.test.ts
import { describe, it, expect } from 'vitest';
import {
  validateFormData,
  transformFormDataToRequest,
  canDeleteEntity,
  computeDerivedValue,
} from '../use-feature-name-domain';

describe('feature-name domain logic', () => {
  describe('validateFormData', () => {
    it('should validate correct data', () => {
      const result = validateFormData({
        field1: 'test',
        field2: 10,
      });

      expect(result.isValid).toBe(true);
      expect(result.errors).toEqual([]);
    });

    it('should catch validation errors', () => {
      const result = validateFormData({
        field1: '',
        field2: -1,
      });

      expect(result.isValid).toBe(false);
      expect(result.errors).toHaveLength(2);
    });
  });

  describe('transformFormDataToRequest', () => {
    it('should transform form data to API format', () => {
      const formData = { field1: '  Test  ', field2: 42 };
      const entities = [{ id: '1' }, { id: '2' }];

      const result = transformFormDataToRequest(formData, entities);

      expect(result).toEqual({
        name: 'Test',
        value: 42,
        entityIds: ['1', '2'],
      });
    });
  });

  describe('canDeleteEntity', () => {
    it('should prevent deletion when business rules violated', () => {
      const entity = { id: '1', status: 'active' };
      const result = canDeleteEntity(entity, []);

      expect(result.canDelete).toBe(false);
      expect(result.reason).toBeDefined();
    });
  });
});
```

**Key Points:**

- No React dependencies - just pure function testing
- Fast execution - no rendering or hook setup
- Easy to write - simple input/output testing
- High confidence - covers all business logic paths

### 2. Hook Unit Tests

Test individual hooks with mocked dependencies.

```typescript
// use-feature-name-form.test.ts
import { renderHook } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { useFeatureNameForm } from '../use-feature-name-form';

describe('useFeatureNameForm', () => {
  it('should initialize with default values', () => {
    const { result } = renderHook(() =>
      useFeatureNameForm({
        existingEntity: undefined,
        initialData: undefined,
      }),
    );

    expect(result.current.control._defaultValues).toEqual({
      field1: '',
      field2: 0,
    });
  });

  it('should initialize with existing entity', () => {
    const existingEntity = { field1: 'test', field2: 42 };
    const { result } = renderHook(() => useFeatureNameForm({ existingEntity }));

    expect(result.current.control._defaultValues).toEqual(existingEntity);
  });
});
```

```typescript
// use-feature-name-actions.test.ts
import { renderHook } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { useFeatureNameActions } from '../use-feature-name-actions';

describe('useFeatureNameActions', () => {
  it('should call domain validation before saving', async () => {
    const mockMutations = {
      create: vi.fn(),
      update: vi.fn(),
      isCreating: false,
      isUpdating: false,
    };

    const mockForm = {
      control: { _formValues: { field1: '', field2: 0 } },
    };

    const mockState = {
      editingIndex: null,
      _setIsDialogOpen: vi.fn(),
    };

    const { result } = renderHook(() =>
      useFeatureNameActions({
        form: mockForm,
        state: mockState,
        data: { entities: [] },
        mutations: mockMutations,
      }),
    );

    // Domain validation should fail for empty field1
    await result.current.handleSave();

    // Mutation should not be called due to validation failure
    expect(mockMutations.create).not.toHaveBeenCalled();
  });
});
```

**Key Points:**

- Test each hook in isolation
- Mock dependencies (other hooks, mutations)
- Verify hook behavior and side effects
- Keep tests focused on single responsibility

### 3. Integration Tests

Test the main orchestrator hook with real sub-hooks.

```typescript
// use-feature-name.test.ts
import { renderHook, waitFor } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { useFeatureName } from '../use-feature-name';

describe('useFeatureName integration', () => {
  it('should orchestrate all sub-hooks correctly', () => {
    const onClose = vi.fn();
    const { result } = renderHook(() =>
      useFeatureName({
        onClose,
        existingEntity: undefined,
      }),
    );

    // Verify all parts are wired together
    expect(result.current.form).toBeDefined();
    expect(result.current.state).toBeDefined();
    expect(result.current.actions).toBeDefined();
    expect(result.current.data).toBeDefined();
  });

  it('should handle full save flow', async () => {
    const onClose = vi.fn();
    const { result } = renderHook(() => useFeatureName({ onClose }));

    // Set valid form data
    result.current.form.setValue('field1', 'test');
    result.current.form.setValue('field2', 42);

    // Trigger save action
    await result.current.actions.handleSave();

    // Verify success callback
    await waitFor(() => {
      expect(onClose).toHaveBeenCalled();
    });
  });
});
```

**Key Points:**

- Test complete workflows end-to-end
- Verify data flows correctly between hooks
- Test critical user journeys
- Can use real or mocked API calls

### 4. Component Tests (Optional)

Test the presentational component with React Testing Library when it has non-trivial rendering logic not already covered by hook tests. This runs in `happy-dom` (no real browser) - it can't verify hover, real layout, or focus order; use an [Interaction Test](#5-interaction-tests-componentspectsx-real-browser) for that.

```typescript
// feature-name.test.tsx
import { render, screen } from '@testing-library/react';
import { FormProvider } from 'react-hook-form';
import { describe, it, expect, vi } from 'vitest';

import { useOscForm } from '@osc/helpers/form';
import { FeatureName } from '../feature-name';

// Stub the heavy child - this suite only exercises FeatureName's own chrome.
vi.mock('../feature-name-heavy-child', () => ({
  FeatureNameHeavyChild: () => <div data-testid="heavy-child" />,
}));

function Host({ title }: Readonly<{ title?: string }>): JSX.Element {
  const methods = useOscForm({ defaultValues: {} });
  return (
    <FormProvider {...methods}>
      <FeatureName title={title} />
    </FormProvider>
  );
}

describe('FeatureName', () => {
  it('renders a title when given one', () => {
    render(<Host title="My Title" />);

    expect(screen.getByText('My Title')).toBeTruthy();
    expect(screen.getByTestId('heavy-child')).toBeTruthy();
  });

  it('omits the title section when none is given', () => {
    render(<Host />);

    expect(screen.queryByText('My Title')).toBeNull();
    // The rest of the component still renders - only the title is dropped.
    expect(screen.getByTestId('heavy-child')).toBeTruthy();
  });
});
```

**Key Points:**

- File is `feature-name.test.tsx` (plain `.test.tsx`, not `.spec.tsx`) - picked up by the `unit` Vitest project alongside domain/hook tests
- `vi.mock` out heavy child components or hooks that aren't the point of the test
- Wrap in whatever providers the component needs (`FormProvider`, a query client, etc.) via a small `Host` helper
- Focus on rendering logic and accessibility, not interaction sequences
- Use sparingly - hook tests already cover most logic

### 5. Interaction Tests (`.component.spec.tsx`, Real Browser)

Runs in a real, headless Chromium via Vitest's browser mode (`@vitest/browser-playwright`), not simulated DOM. Reach for this when behavior depends on something `happy-dom` can't fake: hover, focus order, real CSS layout/animation, portal positioning, keyboard navigation.

```typescript
// feature-name.component.spec.tsx
import { renderComponent } from '@test-utils/render-utils';
import { VIEWPORTS } from '@test-utils/viewports';
import { describe, expect, test } from 'vitest';
import { page } from 'vitest/browser';

import { FeatureName } from '../feature-name';

describe('FeatureName', () => {
  test('clicking Add Item opens the dialog', async () => {
    await renderComponent(<FeatureName />, VIEWPORTS.lg);

    await page.getByRole('button', { name: 'Add Item' }).click();
    await expect.element(page.getByRole('dialog')).toBeInTheDocument();
  });

  test('Save is disabled until the form is valid', async () => {
    await renderComponent(<FeatureName />, VIEWPORTS.lg);

    const saveButton = page.getByRole('button', { name: 'Save' });
    await expect.element(saveButton).toBeDisabled();

    await page.getByLabelText('Field 1').fill('value');
    await expect.element(saveButton).not.toBeDisabled();
  });
});
```

**Key Points:**

- `renderComponent(ui, viewport?)` (from `@test-utils/render-utils`) wraps the component in the app's shared test providers and mounts it to `document.body` via `vitest-browser-react`
- Query and interact through `vitest/browser`'s `page` (Testing-Library-style locators: `getByRole`, `getByLabelText`, `getByTestId`) plus `userEvent` for things `.click()`/`.fill()` don't cover (e.g. `userEvent.hover`)
- Assert with `expect.element(locator).toBe...()` - it auto-retries until the assertion passes or times out
- `VIEWPORTS` (`sm`/`md`/`lg`/`xl`) gives named, consistent viewport sizes instead of magic numbers
- API calls go through MSW - the same mocking layer the dev server and Storybook use, so no separate mocking setup is needed here
- This is what replaces `play` functions inside `.stories.tsx` - which is why Storybook stories in this convention stay play-function-free (see [Storybook Stories Pattern](#storybook-stories-pattern))

### 6. Screenshot Tests (`.screenshot.spec.tsx`, Visual Regression)

Same real-browser setup as interaction tests, plus `toMatchScreenshot`, which pixel-diffs (via `pixelmatch`, 5% threshold) against a baseline PNG. Reach for this when what you're actually verifying is pixels - icon rendering, color, spacing, a complex layout - that would be tedious or impossible to assert structurally.

```typescript
// feature-name.screenshot.spec.tsx
import { renderComponent, waitForTimeout } from '@test-utils/render-utils';
import { VIEWPORTS } from '@test-utils/viewports';
import { expect, test } from 'vitest';
import { page } from 'vitest/browser';

import { FeatureName } from '../feature-name';

async function renderFeature(): Promise<ReturnType<typeof renderComponent>> {
  return renderComponent(<FeatureName />, VIEWPORTS.sm);
}

test('screenshot: default state', async () => {
  await renderFeature();
  await expect
    .element(page.elementLocator(document.body))
    .toMatchScreenshot('default');
});

test('screenshot: hovered', async () => {
  const { locator } = await renderFeature();
  await locator.getByTestId('feature-trigger').hover();
  await waitForTimeout(250); // wait for the hover-open animation
  await expect
    .element(page.elementLocator(document.body))
    .toMatchScreenshot('hovered');
});
```

**Key Points:**

- Baselines live in `__screenshots__/*.png` next to the spec file, named `<spec-file-root>--<arg>.png` - so `'default'` above resolves to `__screenshots__/feature-name--default.png`
- Update baselines locally with `pnpm run test:screenshot:update` after an intentional visual change
- **Must run inside the pinned Docker image** to match CI - see [Running Screenshot Tests in Docker](#running-screenshot-tests-in-docker) below
- Keep each screenshot scoped to the smallest element that captures the thing under test - full-page screenshots are noisy and brittle

### Running Screenshot Tests in Docker

Screenshot tests must run inside the container defined by `Dockerfile.screenshot-tests` (`mcr.microsoft.com/playwright:v1.61.1-noble`, pinned to match `@playwright/test`'s version), forced to the `linux/amd64` platform even on Apple Silicon. Chromium's rasterizer produces different pixels per CPU architecture, so a native Apple-Silicon run would generate baselines that then fail in CI.

`pnpm run test:screenshot:docker [run|update|ui]` wraps the container so you don't have to think about this:

- `run` - headless, mirrors CI exactly (the default)
- `update` - regenerates baselines and copies the changed PNGs back to the host
- `ui` - opens the Vitest browser UI (`http://localhost:51204`) with the full project bind-mounted for HMR

### CI: Screenshot Tests Gate Every PR

The `Screenshot Tests` GitHub Actions workflow runs `pnpm run test:screenshot` (in the same pinned Playwright container) on every PR and on push to `main`. A mismatch fails the check, uploads the actual/baseline/diff PNGs and Playwright traces as artifacts, deploys a diff-review dashboard, and comments on the PR with a link to it.

If the new screenshots are correct, a maintainer comments `/approve-screenshots` on the PR. A separate `Update Screenshots on Comment` workflow verifies the commenter has write access, checks out the PR branch, re-runs the tests in update mode, and pushes the regenerated baselines back onto the PR branch.

### Testing Priority

See [Choosing the Right Test Type](#choosing-the-right-test-type) above for the ranked list (domain → hook → integration → component → interaction → screenshot).

### Testing Principles

- **Prefer the cheapest test that can prove the behavior**: domain/hook > integration > component (`happy-dom`) > interaction (real browser) > screenshot (real browser + pixels)
- **Test behavior, not implementation**: Don't test internal hook details
- **Mock external dependencies**: API calls, toasts, navigation
- **Use meaningful test data**: Realistic examples that reflect business rules
- **Test error paths**: Validation failures, API errors, edge cases
- **Keep tests focused**: One concept per test
- **Fast execution**: Domain function tests should be milliseconds

---

## Best Practices

### Memoization Strategy

- **All callbacks**: Wrap in `useCallback` with correct dependencies
- **Derived state**: Use `useMemo` for expensive computations
- **Callback arrays**: Memoize arrays of callbacks

```typescript
const memoizedCallbacks = useMemo(
  () => items.map((_, index) => () => handleEdit(index)),
  [items, handleEdit],
);
```

### Type Safety

- **Never use `as any`**: Per codebase convention - it defeats the purpose of TypeScript
- **Prefer `type` over `interface`**: Per codebase convention
- **Export all types**: Make types reusable across the feature

### When to Split Further

- **Render helpers**: If component has complex render logic, extract to `use-*-render.tsx`
  - Example: `use-render-controls.tsx` for form field rendering
- **Multiple forms**: Split into separate form hooks
- **Complex domain logic**: Consider splitting domain file by subdomain (e.g., `validation-domain.ts`, `transformation-domain.ts`)
- **Query hooks**: If consolidated query files become too large, consider splitting by domain area (e.g., `workflow-queries.ts`, `alert-queries.ts`)
  - This is a separate concern from feature organization

---

## Creating a New Component from Scratch

Build bottom-up: types → domain logic → hooks (form → state → mutations → actions → orchestrator) → components (both exported from `feature-name.tsx`, per [Component File Structure](#component-file-structure)) → Storybook stories (if worthwhile) → tests. Each hook and the domain module should get its tests as you write it, not at the end.

For the full step-by-step walkthrough with checklists, see [QUICK_START_NEW_COMPONENT.md](./QUICK_START_NEW_COMPONENT.md).

---

## Migration Strategy

**For existing components that need to be refactored to this pattern.**

### Key Differences from Creating New Components

**Migration is exploratory** (discovering what the existing code does while reorganizing it), while **new development is constructive** (building with clear structure from the start). Migrate top-down and incrementally instead of bottom-up: extract business logic to domain functions first, split hooks next, keep the component's external interface stable, and test after each step so existing functionality keeps working throughout.

For the full step-by-step walkthrough, the new-vs-migration comparison table, and the migration checklist, see [QUICK_START_MIGRATION.md](./QUICK_START_MIGRATION.md).

---

## Examples

### Simple Component Structure

For simpler components with minimal state and business logic:

```text
workflow-insights/
├── workflow-insights.tsx                   # Container and presentation (both exported)
├── workflow-insights.stories.tsx           # Stories for presentation
├── use-workflow-insights.ts
├── use-workflow-insights-domain.ts
├── types.ts
└── __tests__/
    ├── workflow-insights-domain.test.ts
    └── use-workflow-insights.test.ts
```

### Complex Component Structure

For complex components with extensive form logic, mutations, and business logic:

```text
rule-recommendations/
├── rule-recommendations-agent-config.tsx                   # Container and presentation (both exported)
├── rule-recommendations-agent-config.stories.tsx           # Stories for presentation
├── use-rule-recommendations-agent-config.ts
├── use-rule-recommendations-agent-config-form.ts
├── use-rule-recommendations-agent-config-state.ts
├── use-rule-recommendations-agent-config-mutations.ts
├── use-rule-recommendations-agent-config-actions.ts
├── use-rule-recommendations-agent-config-domain.ts
├── use-render-controls.tsx
├── types.ts
└── __tests__/
    ├── rule-recommendations-agent-config-domain.test.ts
    ├── use-rule-recommendations-agent-config-form.test.ts
    ├── use-rule-recommendations-agent-config-state.test.ts
    ├── use-rule-recommendations-agent-config-actions.test.ts
    └── use-rule-recommendations-agent-config.test.ts
```

**Notes:**

- Query hooks like `useRuleRecommendations()` are called from consolidated query files
- Container and presentation both live in `-config.tsx` file, both exported
- Presentation component is exported for Storybook stories to import
- Stories catalog the presentation component with mock props for manual verification

### Existing Examples in Codebase

- **Rule Recommendations Agent Config** (`src/pages/agents/rule-recommendations/`)
  - Main reference with multiple supporting hooks
- **Workflow Insights** (`src/pages/home/workflow-insights.tsx`)
  - Simpler pattern with data transformation
- **Attributes Filters** (`src/pages/attributes/use-attributes-filters.ts`)
  - Filter state management pattern

---

## Summary: Key Principles

### SOLID Principles Applied

1. **Single Responsibility**: Each hook has one clear purpose
2. **Open/Closed**: Easy to extend (add new actions) without modifying existing code
3. **Liskov Substitution**: Hooks are interchangeable if they follow the pattern
4. **Interface Segregation**: Component only sees what it needs (via namespacing)
5. **Dependency Inversion**: Actions depend on abstractions (mutation functions), not implementations (mutation objects)

### Pattern Principles

6. **Domain Logic Separation**: Business logic lives in pure functions, separate from React
7. **Container/Presentation Split**: Components split for easy manual verification in Storybook
8. **Linear Dependencies**: Clear flow from domain → props → form → state → data → mutations → actions
9. **No Mutation Exposure**: Mutations wrapped in action callbacks
10. **Boring Orchestrator**: Main hook is just wiring, no logic
11. **Boring Container**: Container just calls hook and renders presentation
12. **Memoize Everything**: Performance optimization through `useCallback`/`useMemo`
13. **Type Safety**: Strong typing with explicit return types, prefer `type` over `interface`
14. **Pure Functions First**: All business logic as side-effect free functions

### Benefits

This convention makes components:

- ✅ **Easier to understand**: Clear separation of concerns with domain logic isolated
- ✅ **Easier to maintain**: Small, focused hooks and pure business logic
- ✅ **Easier to test**: Pure functions test trivially, hooks test independently, presentation component tests with mock props
- ✅ **Easier to extend**: Add new actions/state/logic without touching existing code
- ✅ **More performant**: Proper memoization strategy
- ✅ **Type-safe**: Strong typing throughout
- ✅ **More reusable**: Pure domain functions can be used anywhere
- ✅ **Better coverage**: Business logic tests are fast and comprehensive
- ✅ **Better documented**: Storybook stories serve as a living, manually-browsable catalog
- ✅ **Easier to review**: Designers and stakeholders can interact with components in isolation
- ✅ **Simpler Storybook**: No need to mock hooks, queries, or API calls - just pass plain objects
- ✅ **Manual visual verification**: Toast feedback makes interactions immediately visible when clicking through Storybook

---

## Document Status

- **Version**: 1.5
- **Date**: 2026-08-01
- **Status**: Draft - Open for iteration and feedback
- **Changelog**:
  - v1.5: Added Interaction Tests (`.component.spec.tsx`) and Screenshot Tests (`.screenshot.spec.tsx`) to Testing Strategy, including the `renderComponent()` helper, the Docker/CI pipeline, and the `/approve-screenshots` baseline-update flow; added a "Choosing the Right Test Type" decision guide; rewrote Component Tests with a real `vi.mock`-based example
  - v1.4: Clarified Storybook's role as a manual verification/QA catalog, not an automated test suite; removed `play` function / interaction-testing examples pending a future document on automated screenshot and unit/integration testing
  - v1.3: Added "Creating New Component" section, clarified Storybook is optional when mocking is complex, emphasized domain tests as priority
  - v1.2: Added container/presentation component split for easier Storybook testing
  - v1.1: Added domain logic pattern, comprehensive testing strategy
  - v1.0: Initial version
