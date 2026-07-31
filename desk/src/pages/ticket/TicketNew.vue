<template>
  <div class="flex flex-col overflow-y-auto">
    <LayoutHeader>
      <template #left-header>
        <Breadcrumbs :items="breadcrumbs" />
      </template>
      <template #right-header>
        <CustomActions
          v-if="template.data?._customActions"
          :actions="template.data?._customActions"
        />
      </template>
    </LayoutHeader>
    <!-- Container -->
    <div
      class="flex flex-col gap-5 py-6 h-full flex-1 self-center overflow-auto mx-auto w-full max-w-4xl px-5"
    >
      <!-- custom fields descriptions -->
      <div v-if="Boolean(template.data?.about)" class="">
        <div class="prose-f" v-html="sanitize(template.data.about)" />
      </div>
      <!-- category + SLA policy (customer portal) -->
      <div
        v-if="isCustomerPortal && ticketOptions.data?.types?.length"
        class="flex flex-col gap-4"
      >
        <div class="flex flex-col gap-2">
          <span class="block text-sm text-ink-gray-7">
            {{ __("Category") }}
            <span class="place-self-center text-ink-red-5"> * </span>
          </span>
          <FormControl
            type="select"
            :options="categoryOptions"
            v-model="selectedType"
            :placeholder="__('Select a category')"
          />
        </div>
        <div
          v-if="slaPolicy.data?.applies"
          class="flex flex-col gap-3 rounded border border-outline-gray-2 bg-surface-gray-1 p-4"
        >
          <div class="prose-f text-sm" v-html="sanitize(slaPolicy.data.policy_html)" />
          <div class="flex flex-col gap-2">
            <span class="block text-sm text-ink-gray-7">
              {{ __("Priority level") }}
              <span class="place-self-center text-ink-red-5"> * </span>
            </span>
            <FormControl
              type="select"
              :options="levelOptions"
              v-model="selectedLevel"
              :placeholder="__('Select the level')"
            />
          </div>
        </div>
        <div
          v-else-if="selectedType && slaPolicy.data && !slaPolicy.data.applies"
          class="text-p-sm text-ink-gray-5 rounded border border-outline-gray-2 p-3"
        >
          {{
            __(
              "No SLA applies to this category. It is planned within the ordinary development cycle."
            )
          }}
        </div>
      </div>
      <!-- custom fields -->
      <div
        class="grid grid-cols-1 gap-4 sm:grid-cols-3"
        v-if="Boolean(visibleFields)"
      >
        <UniInput
          v-for="field in visibleFields"
          :key="field.fieldname"
          :field="field"
          :value="templateFields[field.fieldname]"
          @change="
            (e) => handleOnFieldChange(e, field.fieldname, field.fieldtype)
          "
        >
          <template v-if="field.fieldname === 'priority'" #label-extra>
            <template
              v-if="
                ticketPriorityResource.dataMap[templateFields[field.fieldname]]
                  ?.description
              "
            >
              <Tooltip
                :text="
                  ticketPriorityResource.dataMap[
                    templateFields[field.fieldname]
                  ].description.trim()
                "
              >
                <lucide-circle-question-mark class="h-4 w-4 text-ink-gray-6" />
              </Tooltip>
            </template>
          </template>
        </UniInput>
      </div>
      <!-- existing fields -->
      <div
        class="flex flex-col"
        :class="(subject.length >= 2 || description.length) && 'gap-5'"
      >
        <div class="flex flex-col gap-2">
          <span class="block text-sm text-ink-gray-7">
            {{ __("Subject") }}
            <span class="place-self-center text-ink-red-5"> * </span>
          </span>
          <FormControl
            v-model="subject"
            type="text"
            :placeholder="__('A short description')"
            maxlength="140"
          />
        </div>
        <SearchArticles
          v-if="isCustomerPortal"
          :query="subject"
          class="shadow"
        />
        <div v-if="isCustomerPortal">
          <h4
            v-show="subject.length <= 2 && description.length === 0"
            class="text-p-sm text-ink-gray-4 ml-1"
          >
            {{ __("Please enter a subject to continue") }}
          </h4>
          <TicketTextEditor
            v-show="subject.length > 2 || description.length > 0"
            ref="editor"
            v-model:attachments="attachments"
            v-model:content="description"
            :placeholder="__('Detailed explanation')"
            expand
            :uploadFunction="(file:any)=>uploadFunction(file)"
          >
            <template #bottom-right>
              <Button
                :label="__('Submit')"
                theme="gray"
                variant="solid"
                :disabled="
                  $refs.editor?.editor?.isEmpty || ticket.loading || !subject
                "
                @click="() => ticket.submit()"
              />
            </template>
          </TicketTextEditor>
        </div>
      </div>

      <!-- for agent portal -->
      <div v-if="!isCustomerPortal">
        <TicketTextEditor
          ref="editor"
          v-model:attachments="attachments"
          v-model:content="description"
          :placeholder="__('Detailed explanation')"
          expand
          :uploadFunction="(file:any)=>uploadFunction(file)"
        >
          <template #bottom-right>
            <Button
              :label="__('Submit')"
              theme="gray"
              variant="solid"
              :disabled="
                $refs.editor?.editor?.isEmpty || ticket.loading || !subject
              "
              @click="() => ticket.submit()"
            />
          </template>
        </TicketTextEditor>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { LayoutHeader, UniInput } from "@/components";
import {
  handleLinkFieldUpdate,
  handleSelectFieldUpdate,
  parseField,
  setupCustomizations,
} from "@/composables/formCustomisation";
import { useAuthStore } from "@/stores/auth";
import { globalStore } from "@/stores/globalStore";
import { capture } from "@/telemetry";
import { __ } from "@/translation";
import { Field } from "@/types";
import { isCustomerPortal, uploadFunction } from "@/utils";
import {
  Breadcrumbs,
  Button,
  call,
  createListResource,
  createResource,
  FormControl,
  usePageMeta,
} from "frappe-ui";
import { useOnboarding } from "frappe-ui/frappe";
import sanitizeHtml from "sanitize-html";
import {
  computed,
  defineAsyncComponent,
  onMounted,
  reactive,
  ref,
  watch,
} from "vue";
import { useRoute, useRouter } from "vue-router";
import SearchArticles from "../../components/SearchArticles.vue";
const TicketTextEditor = defineAsyncComponent(
  () => import("./TicketTextEditor.vue")
);

interface P {
  templateId?: string;
}

const props = withDefaults(defineProps<P>(), {
  templateId: "",
});

const route = useRoute();
const router = useRouter();
const { $dialog } = globalStore();
const { updateOnboardingStep } = useOnboarding("helpdesk");
const { isManager, userId: userID } = useAuthStore();

const subject = ref("");
const description = ref("");
const attachments = ref([]);
const templateFields = reactive({});

const template = createResource({
  url: "helpdesk.helpdesk.doctype.hd_ticket_template.api.get_one",
  makeParams: () => ({
    name: props.templateId || "Default",
  }),
  auto: true,
  onSuccess: (data) => {
    description.value = data.description_template || "";
    oldFields = window.structuredClone(data.fields || []);
    setupCustomizations(template, {
      doc: templateFields,
      call,
      router,
      $dialog,
      applyFilters,
    });
    setupTemplateFields(data.fields);
  },
});

function setupTemplateFields(fields) {
  fields.forEach((field: Field) => {
    templateFields[field.fieldname] = "";
  });
}

const ticketPriorityResource = createListResource({
  doctype: "HD Ticket Priority",
  fields: ["name", "description"],
  auto: true,
  cache: "ticketPriorities",
});

// customer-facing category + SLA policy shown while opening a ticket
const selectedType = ref("");
const selectedLevel = ref("");

const ticketOptions = createResource({
  url: "fab_helpdesk.api.get_ticket_options",
  auto: isCustomerPortal.value,
  makeParams: () => ({ customer: templateFields["customer"] || "" }),
});

const slaPolicy = createResource({
  url: "fab_helpdesk.api.get_sla_policy",
});

const categoryOptions = computed(() =>
  (ticketOptions.data?.types || []).map((t) => ({
    label: t.label,
    value: t.value,
  }))
);

const levelOptions = computed(() =>
  (slaPolicy.data?.levels || []).map((l) => ({ label: l.label, value: l.value }))
);

watch(selectedType, (val) => {
  templateFields["ticket_type"] = val;
  selectedLevel.value = "";
  templateFields["priority"] = "";
  if (val) {
    slaPolicy.submit({
      ticket_type: val,
      customer: templateFields["customer"] || "",
    });
  }
});

watch(selectedLevel, (val) => {
  templateFields["priority"] = val;
});

let oldFields = [];

function applyFilters(fieldname: string, filters: any = null) {
  const f: Field = template.data.fields.find((f) => f.fieldname === fieldname);
  if (!f) return;
  if (f.fieldtype === "Select") {
    handleSelectFieldUpdate(f, fieldname, filters, templateFields, oldFields);
  } else if (f.fieldtype === "Link") {
    handleLinkFieldUpdate(f, fieldname, filters, templateFields, oldFields);
  }
}

const customOnChange = computed(() => template.data?._customOnChange);

const visibleFields = computed(() => {
  let _fields = template.data?.fields?.filter(
    (f) => !isCustomerPortal.value || !f.hide_from_customer
  );
  if (!_fields) return [];
  return _fields.map((field) => parseField(field, templateFields));
});

function handleOnFieldChange(e: any, fieldname: string, fieldtype: string) {
  templateFields[fieldname] = e.value;
  const fieldDependentFns = customOnChange.value?.[fieldname];
  if (fieldDependentFns) {
    fieldDependentFns.forEach((fn: Function) => {
      fn(e.value, fieldtype);
    });
  }
}

const ticket = createResource({
  url: "helpdesk.helpdesk.doctype.hd_ticket.api.new",
  debounce: 300,
  makeParams: () => ({
    doc: {
      description: description.value,
      subject: subject.value,
      template: props.templateId,
      ...templateFields,
    },
    attachments: attachments.value,
  }),
  validate: (params) => {
    if (isCustomerPortal.value) {
      if (!selectedType.value) return __("Please select a category");
      if (slaPolicy.data?.applies && !selectedLevel.value) {
        return __("Please select a priority level");
      }
    }
    const fields = visibleFields.value?.filter((f) => f.required) || [];
    const toVerify = [...fields, "subject", "description"];
    for (const field of toVerify) {
      if (!params.doc[field.fieldname || field]) {
        return `${field.label || field} is required`;
      }
    }
  },
  onSuccess: (data) => {
    router.push({
      name: isCustomerPortal.value ? "TicketCustomer" : "TicketAgent",
      params: {
        ticketId: data.name,
      },
    });
    if (isManager) {
      updateOnboardingStep("create_first_ticket", true, false, () =>
        localStorage.setItem("firstTicket", data.name)
      );
    }
  },
});

function sanitize(html: string) {
  return sanitizeHtml(html, {
    allowedTags: sanitizeHtml.defaults.allowedTags.concat(["img"]),
  });
}

const breadcrumbs = computed(() => {
  const items = [
    {
      label: __("Tickets"),
      route: {
        name: isCustomerPortal.value ? "TicketsCustomer" : "TicketsAgent",
      },
    },
    {
      label: __("New Ticket"),
      route: {
        name: "TicketNew",
      },
    },
  ];
  return items;
});

usePageMeta(() => ({
  title: __("New Ticket"),
}));

onMounted(() => {
  capture("new_ticket_page", {
    data: {
      user: userID,
    },
  });
});
</script>
