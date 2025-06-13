In order for the jaxb2-maven-plugin to work, a few manual adjustmetns were made to the original pubmed_250101.dtd file.

These changes are made in response to "org.xml.sax.SAXParseException: Either an attribute declaration or ">" is expected, not ">""

Angle brackets that are on a line by themselves were moved to the line above.