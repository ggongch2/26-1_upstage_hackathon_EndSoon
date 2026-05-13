<?xml version="1.0" encoding="UTF-8"?>
<!--
  Minimal MathML → OMML transform for STEM textbook demos.
  Covers: mi, mn, mo, mtext, mrow, mfrac, msup, msub, msubsup,
  msqrt, mroot, mfenced, mtable/mtr/mtd, mover/munder/munderover.

  This is NOT a full implementation. For production fidelity replace
  with Microsoft's MML2OMML.XSL (distributed with MS Office) or a
  more complete open-source transform.
-->
<xsl:stylesheet version="1.0"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:mml="http://www.w3.org/1998/Math/MathML"
    xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"
    exclude-result-prefixes="mml">

  <xsl:output method="xml" indent="no" omit-xml-declaration="yes"/>

  <xsl:template match="/mml:math | /math">
    <m:oMath>
      <xsl:apply-templates/>
    </m:oMath>
  </xsl:template>

  <xsl:template match="mml:mrow | mrow">
    <xsl:apply-templates/>
  </xsl:template>

  <xsl:template match="mml:mi | mi">
    <m:r>
      <m:rPr><m:sty m:val="i"/></m:rPr>
      <m:t><xsl:value-of select="."/></m:t>
    </m:r>
  </xsl:template>

  <xsl:template match="mml:mn | mn">
    <m:r><m:t><xsl:value-of select="."/></m:t></m:r>
  </xsl:template>

  <xsl:template match="mml:mo | mo">
    <m:r><m:t><xsl:value-of select="."/></m:t></m:r>
  </xsl:template>

  <xsl:template match="mml:mtext | mtext">
    <m:r><m:t><xsl:value-of select="."/></m:t></m:r>
  </xsl:template>

  <xsl:template match="mml:mfrac | mfrac">
    <m:f>
      <m:fPr><m:type m:val="bar"/></m:fPr>
      <m:num><xsl:apply-templates select="*[1]"/></m:num>
      <m:den><xsl:apply-templates select="*[2]"/></m:den>
    </m:f>
  </xsl:template>

  <xsl:template match="mml:msup | msup">
    <m:sSup>
      <m:e><xsl:apply-templates select="*[1]"/></m:e>
      <m:sup><xsl:apply-templates select="*[2]"/></m:sup>
    </m:sSup>
  </xsl:template>

  <xsl:template match="mml:msub | msub">
    <m:sSub>
      <m:e><xsl:apply-templates select="*[1]"/></m:e>
      <m:sub><xsl:apply-templates select="*[2]"/></m:sub>
    </m:sSub>
  </xsl:template>

  <xsl:template match="mml:msubsup | msubsup">
    <m:sSubSup>
      <m:e><xsl:apply-templates select="*[1]"/></m:e>
      <m:sub><xsl:apply-templates select="*[2]"/></m:sub>
      <m:sup><xsl:apply-templates select="*[3]"/></m:sup>
    </m:sSubSup>
  </xsl:template>

  <xsl:template match="mml:msqrt | msqrt">
    <m:rad>
      <m:radPr><m:degHide m:val="1"/></m:radPr>
      <m:deg/>
      <m:e><xsl:apply-templates/></m:e>
    </m:rad>
  </xsl:template>

  <xsl:template match="mml:mroot | mroot">
    <m:rad>
      <m:radPr><m:degHide m:val="0"/></m:radPr>
      <m:deg><xsl:apply-templates select="*[2]"/></m:deg>
      <m:e><xsl:apply-templates select="*[1]"/></m:e>
    </m:rad>
  </xsl:template>

  <xsl:template match="mml:mfenced | mfenced">
    <m:d>
      <m:dPr>
        <m:begChr m:val="{@open}"/>
        <m:endChr m:val="{@close}"/>
      </m:dPr>
      <m:e><xsl:apply-templates/></m:e>
    </m:d>
  </xsl:template>

  <xsl:template match="mml:mover | mover">
    <m:acc>
      <m:accPr><m:chr m:val="{string(*[2])}"/></m:accPr>
      <m:e><xsl:apply-templates select="*[1]"/></m:e>
    </m:acc>
  </xsl:template>

  <xsl:template match="mml:munder | munder">
    <m:limLow>
      <m:e><xsl:apply-templates select="*[1]"/></m:e>
      <m:lim><xsl:apply-templates select="*[2]"/></m:lim>
    </m:limLow>
  </xsl:template>

  <xsl:template match="mml:munderover | munderover">
    <m:nary>
      <m:naryPr>
        <m:chr m:val="{string(*[1])}"/>
      </m:naryPr>
      <m:sub><xsl:apply-templates select="*[2]"/></m:sub>
      <m:sup><xsl:apply-templates select="*[3]"/></m:sup>
      <m:e/>
    </m:nary>
  </xsl:template>

  <xsl:template match="mml:mtable | mtable">
    <m:m>
      <m:mPr>
        <m:mcs>
          <m:mc>
            <m:mcPr>
              <m:count m:val="{count(*[1]/*)}"/>
              <m:mcJc m:val="center"/>
            </m:mcPr>
          </m:mc>
        </m:mcs>
      </m:mPr>
      <xsl:apply-templates/>
    </m:m>
  </xsl:template>

  <xsl:template match="mml:mtr | mtr">
    <m:mr>
      <xsl:apply-templates/>
    </m:mr>
  </xsl:template>

  <xsl:template match="mml:mtd | mtd">
    <m:e>
      <xsl:apply-templates/>
    </m:e>
  </xsl:template>

  <xsl:template match="mml:mspace | mspace">
    <m:r><m:t> </m:t></m:r>
  </xsl:template>

  <xsl:template match="*">
    <xsl:apply-templates/>
  </xsl:template>

</xsl:stylesheet>
