# ExampleCo Widgets: how an order flows (source notes)

Synthetic fixture for the Helmet Write walkthrough. ExampleCo is a fictional
company; every detail below is invented.

## Steps, in order

1. A customer builds an order on the order screen, online or offline.
2. An order placed offline waits in a queue on the device.
3. When a connection is available, the device sends the order to the ExampleCo
   order service. Online orders go there directly.
4. The order service checks stock and confirms or rejects the order.
5. A confirmed order appears in the customer's shared order list within one
   minute (4.1 and later).

## Facts the diagram must keep

- There are exactly two ways in: online and offline.
- Only the order service confirms or rejects an order.
- Shared order lists exist from Widgets 4.1 (9 September).

## Plans for Q4 (for context only; belongs to another section)

- Widgets 4.2 is planned for November and adds barcode scanning.
- No other Q4 release dates are committed.
