# provider-core 0.5.16

The shared tool conversation now imports and runs with injected carriers in a native-only installation, without HTTP dependencies. Chat and exchange failure definitions live in lightweight modules; existing public imports expose the same exception types and retain their evidence fields.

Actual HTTP chat use still requires the API extra. Native and API installation tests cover both the dependency boundary and public exception identity.
