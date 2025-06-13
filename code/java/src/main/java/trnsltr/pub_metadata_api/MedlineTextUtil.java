package trnsltr.pub_metadata_api;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collection;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Stack;

import org.medline.Abstract;
import org.medline.AbstractText;
import org.medline.MedlineCitation;
import org.medline.PubmedArticle;

import edu.ucdenver.ccp.common.collections.CollectionsUtil;
import edu.ucdenver.ccp.common.collections.CollectionsUtil.SortOrder;
import edu.ucdenver.ccp.nlp.core.annotation.Span;
import edu.ucdenver.ccp.nlp.core.annotation.TextAnnotation;
import edu.ucdenver.ccp.nlp.core.annotation.TextAnnotationFactory;
import lombok.Data;

public class MedlineTextUtil {

	public static String extractTitleText(MedlineCitation medlineCitation, List<TextAnnotation> titleAnnotations) {
		return processTitleAndAbstractText(medlineCitation.getArticle().getArticleTitle().getvalue(), titleAnnotations,
				medlineCitation.getPMID().getvalue());
	}

	/**
	 * @param pubmedArticle
	 * @param annotations
	 * @param
	 * @return the abstract text compiled from the {@link PubmedArticle}
	 */
	public static String getAbstractText(PubmedArticle pubmedArticle, List<TextAnnotation> annotations) {
		Abstract theAbstract = pubmedArticle.getMedlineCitation().getArticle().getAbstract();
		if (theAbstract == null) {
			return null;
		}
		StringBuilder sb = new StringBuilder();
		for (AbstractText aText : theAbstract.getAbstractText()) {
			String text = aText.getvalue();

			if (sb.length() == 0) {
				sb.append(text);
			} else {
				sb.append("\n" + text);
			}
		}

		return processTitleAndAbstractText(sb.toString(), annotations,
				pubmedArticle.getMedlineCitation().getPMID().getvalue());
	}

	/**
	 * Replace multiple whitespace with single space. Remove <b>, <i>, <u>, <sup>,
	 * <sub>
	 * 
	 * @param observedAnnotations annotations indicating where sub- and superscript
	 *                            text was observed in the input text (title or
	 *                            abstract)
	 * @param docId
	 * 
	 * @param aText
	 * @return
	 */
	private static String processTitleAndAbstractText(String text, List<TextAnnotation> observedAnnotations,
			String docId) {
		String updatedText = text.trim();
		// below is a special space being replaced by a regular space
		updatedText = updatedText.replaceAll(" ", " ");

		// there some records that have line breaks and lots of extra spaces -- these
		// need to be treated differently when the line break occurs just prior to or
		// just after a tag. See https://pubmed.ncbi.nlm.nih.gov/31000267/
		updatedText = updatedText.replaceAll("\\n\\s+<", "<");
		updatedText = updatedText.replaceAll(">\\n\\s+", ">");

		updatedText = updatedText.replaceAll("<b>", "");
		updatedText = updatedText.replaceAll("</b>", "");
		updatedText = updatedText.replaceAll("<i>", "");
		updatedText = updatedText.replaceAll("</i>", "");
		updatedText = updatedText.replaceAll("<u>", "");
		updatedText = updatedText.replaceAll("</u>", "");
		updatedText = updatedText.replaceAll("\\s\\s+", " ");

		updatedText = updatedText.replaceAll("&lt;", "<");
		updatedText = updatedText.replaceAll("&gt;", ">");
		updatedText = updatedText.replaceAll("&amp;", "&");
		updatedText = updatedText.replaceAll("&quot;", "\"");
		updatedText = updatedText.replaceAll("&apos;", "'");

		/*
		 * once we have replaced the above HTML tags and escaped XML characters, we will
		 * replace the subscript and superscript tags. While doing so, we will add
		 * "section" annotations for these tags so that downstream tools will be able to
		 * recover the subscript and superscript information.
		 */

		observedAnnotations.addAll(extractSubNSuperscriptAnnotations(updatedText, docId));
		updatedText = updatedText.replaceAll("<sub>", "");
		updatedText = updatedText.replaceAll("</sub>", "");
		updatedText = updatedText.replaceAll("<sup>", "");
		updatedText = updatedText.replaceAll("</sup>", "");
		updatedText = updatedText.replaceAll("<sub/>", "");
		updatedText = updatedText.replaceAll("<sup/>", "");
		validateObservedAnnotations(observedAnnotations, updatedText);
		return updatedText;
	}

	/**
	 * Creates annotations indicating where sub- and superscript text was observed
	 * in the specified text
	 * 
	 * @param text
	 * @return
	 */
	private static Collection<? extends TextAnnotation> extractSubNSuperscriptAnnotations(String text, String docId) {
		List<TextAnnotation> annots = new ArrayList<TextAnnotation>();
		TextAnnotationFactory factory = TextAnnotationFactory.createFactoryWithDefaults();

		String updatedText = text;
		// handle <sub/> just in case?
		List<String> tags = Arrays.asList("sup", "sub");
		Stack<TextAnnotation> stack = new Stack<TextAnnotation>();
		while (containsTag(updatedText, tags)) {
			Tag tag = getNextTag(updatedText, tags);
			if (tag.getType() == Tag.Type.OPEN) {
				// add new annotation to the stack
				TextAnnotation annot = factory.createAnnotation(tag.getStart(), tag.getStart() + 1, "",
						tag.getTagText());
				stack.push(annot);
			} else if (!stack.isEmpty() && tag.getType() != Tag.Type.EMPTY) {
				// pop the stack, complete the annotation and add it to the annots list unless
				// this is an empty tag, e.g. <sub/>, or if the stack is empty. There is at
				// least one example of a </sup> tag that did not have a <sup> tag:
				// https://pubmed.ncbi.nlm.nih.gov/12897808/

				TextAnnotation annot = stack.pop();

				// validate that the tag type of the popped annot matches the expected tag type
				if (!tag.getTagText().equals(annot.getClassMention().getMentionName())) {
					System.err.println(String.format("Popped annot not of expected type: %s != %s for document %s",
							tag.getTagText(), annot.getClassMention().getMentionName(), docId));
				} else {
					int end = tag.getStart();
					int start = annot.getAnnotationSpanStart();
					annot.setSpan(new Span(start, end));
					annot.setCoveredText(updatedText.substring(start, end));
					annots.add(annot);
				}
			}
			// update the text by removing the tag that was just processed
			updatedText = updatedText.replaceFirst(tag.getRegex(), "");
		}

		/*
		 * the stack should be empty at this point - but may not be, e.g.
		 * https://pubmed.ncbi.nlm.nih.gov/23037387/ has a sub tag that is never closed.
		 * Even if it's not empty, we will just move on and return the annotations that
		 * were generated.
		 */
//		if (!stack.isEmpty()) {
//			throw new IllegalStateException("Annotation stack should be empty at this point. " + docId);
//		}

		return annots;
	}

	/**
	 * Makes sure that the annotations created to store sub- and superscript
	 * information use spans that align with the document text.
	 * 
	 * @param observedAnnotations
	 * @param updatedText
	 */
	private static void validateObservedAnnotations(List<TextAnnotation> observedAnnotations, String updatedText) {
		for (TextAnnotation annot : observedAnnotations) {
			String expectedText = annot.getCoveredText();
			String observedText = updatedText.substring(annot.getAnnotationSpanStart(), annot.getAnnotationSpanEnd());
			if (!observedText.equals(expectedText)) {
				throw new IllegalStateException(String.format(
						"Error during Medline XML parsing - extracted annotation text (likely for sub or superscript "
								+ "annotation) does not match as expected. '%s' != '%s'",
						expectedText, observedText));
			}
		}

	}

	private static Tag getNextTag(String text, List<String> tags) {
		Map<Integer, Tag> startIndexToTagMap = new HashMap<Integer, Tag>();
		for (String tagStr : tags) {
			for (Tag.Type type : Tag.Type.values()) {
				Tag tag = new Tag(tagStr, type);
				int index = text.indexOf(tag.getHtmlTag());
				if (index >= 0) {
					// unit tests suggested -1 needed here.
					tag.setStart(index);
					startIndexToTagMap.put(index, tag);
				}
			}
		}

		/* there needs to be at least one tag in the map */
		if (startIndexToTagMap.size() < 1) {
			throw new IllegalStateException("Expected to find tag, but did not.");
		}

		/* sort the map by tag start index, then choose the first tag and return */
		Map<Integer, Tag> sortedMap = CollectionsUtil.sortMapByKeys(startIndexToTagMap, SortOrder.ASCENDING);

		/* return the first tag */
		return sortedMap.entrySet().iterator().next().getValue();

	}

	private static boolean containsTag(String text, List<String> tags) {
		for (String tagStr : tags) {
			for (Tag.Type type : Tag.Type.values()) {
				Tag tag = new Tag(tagStr, type);
				if (text.contains(tag.getHtmlTag())) {
					return true;
				}
			}
		}
		return false;
	}

	@Data
	private static class Tag {
		public enum Type {
			OPEN, CLOSE, EMPTY;
		}

		private int start;
		private final String tagText;
		private final Type type;

		private String getHtmlTag() {
			return String.format("<%s%s%s>", (type == Type.CLOSE ? "/" : ""), tagText, (type == Type.EMPTY ? "/" : ""));
		}

		private String getRegex() {
			return getHtmlTag();
		}
	}
}
